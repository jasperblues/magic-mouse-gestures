#!/usr/bin/python3
"""Magic Mouse gestures and smooth scrolling for GNOME on Linux.

The hid-magicmouse kernel driver reports every finger on the Magic Mouse's touch
surface (ABS_MT_* events), but libinput treats the device as a plain mouse, GNOME
only knows about touchpad gestures, and the driver's own scroll emulation is
coarse wheel clicks with no momentum. This daemon fills the gap:

  * one-finger scroll           -> smooth high-resolution scrolling with momentum
  * two-finger double-tap       -> toggle the Activities overview (Mission Control)
  * two-finger swipe left/right -> switch workspace (like swiping between Spaces)

It grabs the Magic Mouse exclusively and re-emits pointer motion and buttons
through a virtual clone. The driver's scroll events are dropped and replaced by
scrolling computed from the raw touch data.
"""

import argparse
import errno
import glob
import logging
import math
import os
import select
import time
from collections import deque

import libevdev
from gi.repository import Gio, GLib

log = logging.getLogger("magic-mouse-gestures")

APPLE_VENDORS = {0x004C, 0x05AC}  # Bluetooth SIG vendor ID, USB vendor ID
MAGIC_MOUSE_PRODUCTS = {0x030D, 0x0269, 0x0323}  # Magic Mouse 1, 2, USB-C
CLONE_SUFFIX = " (gestures)"

# Gesture tuning, in the driver's touch units (surface is roughly 2400 x 3600) and seconds.
TAP_MAX_DURATION = 0.30   # a tap's fingers lift within this time
TAP_MAX_MOVE = 120        # ...and move less than this
DOUBLE_TAP_WINDOW = 0.60  # max gap between the two taps of a double-tap
DOUBLE_TAP_ACTIONS = {2: "toggle_overview"}  # fingers -> Gnome method
SWIPE_MIN_DX = 620        # horizontal travel (average of both fingers) to count as a swipe
SWIPE_MIN_RATIO = 1.5     # |dx| must exceed |dy| by this factor (swipes arc a little)

# Pointer tuning: scale raw motion before GNOME's acceleration sees it, for finer control
# than GNOME's lowest speed setting allows. 1.0 = unchanged.
POINTER_SCALE = 0.55

# Scroll tuning. Output is in hi-res wheel units: 120 = one classic wheel notch.
SCROLL_GAIN = 1.0         # hi-res units per touch unit of finger travel
SCROLL_DEADZONE = 40      # finger travel before scrolling starts (ignores click wobble)
SCROLL_AXIS_LOCK = 2.0    # lock to one axis when it dominates the start of a scroll by this factor
SCROLL_SIGN_Y = -1        # flip if vertical scrolling runs the wrong way
SCROLL_SIGN_X = 1         # flip if horizontal scrolling runs the wrong way
MOMENTUM_WINDOW = 0.06    # seconds of recent motion used to measure fling speed
MOMENTUM_MIN_SPEED = 600  # hi-res units/s needed at lift-off to start momentum
MOMENTUM_STOP_SPEED = 40  # momentum ends below this speed
MOMENTUM_TAU = 0.35       # momentum decay time constant (larger = glides further)
TICK = 0.008              # momentum frame interval

MOTION_CODES = {libevdev.EV_REL.REL_X: 0, libevdev.EV_REL.REL_Y: 1}
WHEEL_CODES = {libevdev.EV_REL.REL_WHEEL, libevdev.EV_REL.REL_WHEEL_HI_RES,
               libevdev.EV_REL.REL_HWHEEL, libevdev.EV_REL.REL_HWHEEL_HI_RES}
BUTTONS = {libevdev.EV_KEY.BTN_LEFT, libevdev.EV_KEY.BTN_RIGHT, libevdev.EV_KEY.BTN_MIDDLE}
SYN = libevdev.InputEvent(libevdev.EV_SYN.SYN_REPORT, 0)


class Contact:
    def __init__(self, now):
        self.start = now
        self.x0 = self.y0 = self.x = self.y = None

    def update(self, axis, value):
        if axis == "x":
            self.x = value
            if self.x0 is None:
                self.x0 = value
        else:
            self.y = value
            if self.y0 is None:
                self.y0 = value

    @property
    def dx(self):
        return 0 if self.x0 is None else self.x - self.x0

    @property
    def dy(self):
        return 0 if self.y0 is None else self.y - self.y0


class Gnome:
    """Actions on the GNOME Shell session."""

    def __init__(self, keyboard):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.keyboard = keyboard

    def _overview(self, method, *args):
        return self.bus.call_sync(
            "org.gnome.Shell", "/org/gnome/Shell", "org.freedesktop.DBus.Properties",
            method, GLib.Variant("(ss" + "v" * len(args) + ")", ("org.gnome.Shell", "OverviewActive", *args)),
            None, Gio.DBusCallFlags.NONE, 1000, None)

    def toggle_overview(self):
        active = self._overview("Get").unpack()[0]
        self._overview("Set", GLib.Variant("b", not active))
        log.info("overview %s", "closed" if active else "opened")

    def _shortcut(self, *keys):
        for code in keys:
            self.keyboard.send_events([libevdev.InputEvent(code, 1), SYN])
        for code in reversed(keys):
            self.keyboard.send_events([libevdev.InputEvent(code, 0), SYN])

    def switch_workspace(self, direction):
        # GNOME's default Super+PageUp / Super+PageDown workspace bindings.
        key = libevdev.EV_KEY.KEY_PAGEDOWN if direction == "right" else libevdev.EV_KEY.KEY_PAGEUP
        self._shortcut(libevdev.EV_KEY.KEY_LEFTMETA, key)
        log.info("workspace %s", direction)


class Scroller:
    """Turns single-finger touch motion into smooth hi-res wheel events with momentum."""

    def __init__(self, clone):
        self.clone = clone
        self.acc = [0.0, 0.0]      # sub-unit remainders (x, y)
        self.notch = [0, 0]        # hi-res units since the last classic notch (x, y)
        self.reset_touch()
        self.velocity = None       # momentum (vx, vy) in hi-res units/s
        self.last_tick = 0.0

    def reset_touch(self):
        self.scrolling = False
        self.axis = None           # None = free, "x" or "y" = locked
        self.last = None
        self.samples = deque()

    def stop_momentum(self):
        self.velocity = None

    def emit(self, hx, hy):
        events = []
        for i, (value, hires, lores) in enumerate((
                (hx, libevdev.EV_REL.REL_HWHEEL_HI_RES, libevdev.EV_REL.REL_HWHEEL),
                (hy, libevdev.EV_REL.REL_WHEEL_HI_RES, libevdev.EV_REL.REL_WHEEL))):
            self.acc[i] += value
            whole = int(self.acc[i])
            if not whole:
                continue
            self.acc[i] -= whole
            events.append(libevdev.InputEvent(hires, whole))
            self.notch[i] += whole
            notches = int(self.notch[i] / 120)
            if notches:
                self.notch[i] -= notches * 120
                events.append(libevdev.InputEvent(lores, notches))
        if events:
            self.clone.send_events(events + [SYN])

    def touch(self, contact, now):
        """Called each frame while exactly one finger is on the surface."""
        if contact.x is None or contact.y is None:
            return
        if not self.scrolling:
            ddx, ddy = abs(contact.dx), abs(contact.dy)
            if max(ddx, ddy) < SCROLL_DEADZONE:
                return
            self.scrolling = True
            self.stop_momentum()
            if ddy >= SCROLL_AXIS_LOCK * ddx:
                self.axis = "y"
            elif ddx >= SCROLL_AXIS_LOCK * ddy:
                self.axis = "x"
            self.last = (contact.x, contact.y)
            return
        dx, dy = contact.x - self.last[0], contact.y - self.last[1]
        self.last = (contact.x, contact.y)
        hx = 0.0 if self.axis == "y" else dx * SCROLL_GAIN * SCROLL_SIGN_X
        hy = 0.0 if self.axis == "x" else dy * SCROLL_GAIN * SCROLL_SIGN_Y
        self.emit(hx, hy)
        self.samples.append((now, hx, hy))
        while self.samples and now - self.samples[0][0] > MOMENTUM_WINDOW:
            self.samples.popleft()

    def release(self, now):
        """Called when the scrolling finger lifts: start momentum if it was flung."""
        if self.scrolling and len(self.samples) >= 2:
            span = max(now - self.samples[0][0], 0.016)
            vx = sum(s[1] for s in self.samples) / span
            vy = sum(s[2] for s in self.samples) / span
            if math.hypot(vx, vy) >= MOMENTUM_MIN_SPEED:
                self.velocity = (vx, vy)
                self.last_tick = now
        self.reset_touch()

    def tick(self, now):
        if not self.velocity:
            return
        dt = now - self.last_tick
        self.last_tick = now
        vx, vy = self.velocity
        self.emit(vx * dt, vy * dt)
        decay = math.exp(-dt / MOMENTUM_TAU)
        vx, vy = vx * decay, vy * decay
        self.velocity = (vx, vy) if math.hypot(vx, vy) >= MOMENTUM_STOP_SPEED else None


class GestureTracker:
    def __init__(self, gnome, scroller):
        self.gnome = gnome
        self.scroller = scroller
        self.slot = 0
        self.contacts = {}       # slot -> Contact
        self.session_max = 0     # most fingers down at once in the current session
        self.session_contacts = []
        self.swiped = False
        self.last_tap = 0.0
        self.last_tap_fingers = 0
        self.buttons_down = 0

    def handle(self, ev, now):
        code = ev.code
        if code in BUTTONS:
            self.buttons_down += 1 if ev.value else -1
            self.buttons_down = max(self.buttons_down, 0)
            self.scroller.stop_momentum()
            self.scroller.reset_touch()
            # Re-anchor so finger wobble during the click doesn't count as scroll travel.
            for c in self.contacts.values():
                c.x0, c.y0 = c.x, c.y
        elif code == libevdev.EV_ABS.ABS_MT_SLOT:
            self.slot = ev.value
        elif code == libevdev.EV_ABS.ABS_MT_TRACKING_ID:
            if ev.value >= 0:
                if not self.contacts:
                    self.session_max, self.session_contacts, self.swiped = 0, [], False
                self.scroller.stop_momentum()
                c = Contact(now)
                self.contacts[self.slot] = c
                self.session_contacts.append(c)
                self.session_max = max(self.session_max, len(self.contacts))
                if len(self.contacts) > 1:
                    self.scroller.reset_touch()
            else:
                self.contacts.pop(self.slot, None)
                if not self.contacts:
                    if self.session_max == 1:
                        self.scroller.release(now)
                    else:
                        self.scroller.reset_touch()
                    self.end_session(now)
        elif code in (libevdev.EV_ABS.ABS_MT_POSITION_X, libevdev.EV_ABS.ABS_MT_POSITION_Y):
            c = self.contacts.get(self.slot)
            if c:
                c.update("x" if code == libevdev.EV_ABS.ABS_MT_POSITION_X else "y", ev.value)

    def frame_done(self, now):
        if len(self.contacts) == 1 and self.session_max == 1 and not self.buttons_down:
            self.scroller.touch(next(iter(self.contacts.values())), now)
        self.check_swipe()

    def check_swipe(self):
        if self.swiped or len(self.contacts) != 2:
            return
        a, b = self.contacts.values()
        if (a.dx > 0) != (b.dx > 0) or min(abs(a.dx), abs(b.dx)) < SWIPE_MIN_DX * 0.7:
            return
        dx, dy = (a.dx + b.dx) / 2, (a.dy + b.dy) / 2
        if abs(dx) >= SWIPE_MIN_DX and abs(dx) >= SWIPE_MIN_RATIO * abs(dy):
            self.swiped = True
            self.last_tap = 0.0
            # Natural direction, as on macOS: fingers move left -> go to the workspace on the right.
            self.gnome.switch_workspace("right" if dx < 0 else "left")

    def end_session(self, now):
        cs = self.session_contacts
        n = self.session_max
        if self.swiped or n not in DOUBLE_TAP_ACTIONS or len(cs) != n:
            return
        is_tap = all(now - c.start <= TAP_MAX_DURATION and max(abs(c.dx), abs(c.dy)) <= TAP_MAX_MOVE for c in cs)
        if not is_tap:
            return
        if self.last_tap_fingers == n and now - self.last_tap <= DOUBLE_TAP_WINDOW:
            self.last_tap = 0.0
            getattr(self.gnome, DOUBLE_TAP_ACTIONS[n])()
        else:
            self.last_tap, self.last_tap_fingers = now, n


def make_keyboard():
    dev = libevdev.Device()
    dev.name = "Magic Mouse gestures (keys)"
    # Deliberately not a full keyboard, so keyboard remappers (Toshy/xwaykeyz) don't grab it.
    for code in (libevdev.EV_KEY.KEY_LEFTMETA, libevdev.EV_KEY.KEY_PAGEUP, libevdev.EV_KEY.KEY_PAGEDOWN):
        dev.enable(code)
    return dev.create_uinput_device()


def find_magic_mouse():
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            with open(path, "rb") as fd:
                dev = libevdev.Device(fd)
                ident = dev.id
                if (ident["vendor"] in APPLE_VENDORS and ident["product"] in MAGIC_MOUSE_PRODUCTS
                        and not dev.name.endswith(CLONE_SUFFIX)
                        and dev.has(libevdev.EV_ABS.ABS_MT_POSITION_X)):
                    return path
        except OSError:
            continue
    return None


def run(path, gnome):
    with open(path, "rb") as fd:
        os.set_blocking(fd.fileno(), False)
        dev = libevdev.Device(fd)
        log.info("using %s (%s)", path, dev.name)
        dev.grab()
        dev.name = dev.name + CLONE_SUFFIX
        clone = dev.create_uinput_device()
        scroller = Scroller(clone)
        motion_rem = [0.0, 0.0]   # fractional pointer motion carried between frames
        tracker = GestureTracker(gnome, scroller)
        frame = []
        try:
            while True:
                timeout = TICK if scroller.velocity else None
                ready, _, _ = select.select([fd], [], [], timeout)
                if ready:
                    try:
                        for ev in dev.events():
                            if ev.code != libevdev.EV_SYN.SYN_REPORT:
                                frame.append(ev)
                                continue
                            now = time.monotonic()
                            for e in frame:
                                tracker.handle(e, now)
                            tracker.frame_done(now)
                            out = []
                            for e in frame:
                                if e.code in WHEEL_CODES:
                                    continue
                                axis = MOTION_CODES.get(e.code)
                                if axis is not None and POINTER_SCALE != 1.0:
                                    motion_rem[axis] += e.value * POINTER_SCALE
                                    whole = int(motion_rem[axis])
                                    motion_rem[axis] -= whole
                                    if not whole:
                                        continue
                                    e = libevdev.InputEvent(e.code, whole)
                                out.append(e)
                            if out:
                                clone.send_events(out + [ev])
                            frame = []
                    except libevdev.EventsDroppedException:
                        frame = []
                        for _ in dev.sync():
                            pass
                scroller.tick(time.monotonic())
        finally:
            try:
                dev.ungrab()
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    gnome = Gnome(make_keyboard())
    waiting = False
    while True:
        path = find_magic_mouse()
        if not path:
            if not waiting:
                log.info("waiting for a Magic Mouse...")
                waiting = True
            time.sleep(2)
            continue
        waiting = False
        try:
            run(path, gnome)
        except OSError as e:
            if e.errno != errno.ENODEV:
                raise
            log.info("Magic Mouse disconnected")


if __name__ == "__main__":
    main()
