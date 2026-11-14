# magic-mouse-gestures

**Make an Apple Magic Mouse feel like it does on a Mac — on GNOME/Linux.**
Smooth one-finger scrolling with momentum, two-finger double-tap for the
overview (Mission Control), and two-finger swipes to switch workspaces (Spaces).
One small Python daemon, no root at runtime, no kernel changes.

```sh
sudo dnf install python3-libevdev python3-gobject      # or your distro's equivalent
git clone https://github.com/jasperblues/magic-mouse-gestures ~/projects/magic-mouse-gestures
systemctl --user enable --now ~/projects/magic-mouse-gestures/magic-mouse-gestures.service
```

| Gesture | Action |
|---|---|
| One-finger scroll | Smooth high-resolution scrolling, with momentum after a flick |
| Two-finger double-tap | Toggle the Activities overview (like Mission Control) |
| Two-finger swipe left/right | Switch workspace, natural direction (like Spaces) |

## Why

Out of the box on Linux, the Magic Mouse is a poor experience:

- **Scrolling is coarse and has no momentum.** The `hid-magicmouse` driver turns
  finger movement into old-style wheel clicks.
- **No gestures.** libinput treats the device as a plain mouse, and GNOME only
  supports gestures on touchpads.

Yet the driver already reports every finger on the touch surface. All the data
needed for a Mac-like experience is there — nothing was using it. This daemon
does.

## How it works

- Grabs the Magic Mouse exclusively and re-emits pointer motion and buttons
  through a virtual clone (`<name> (gestures)`), so everything else behaves as
  before.
- Drops the driver's wheel events and computes scrolling from the raw
  multitouch data instead: hi-res wheel events, plus a decaying momentum glide
  after a flick.
- Overview: sets the `OverviewActive` property on `org.gnome.Shell` over D-Bus.
- Workspaces: sends GNOME's default `Super+Page Up` / `Super+Page Down` from a
  minimal virtual keyboard (deliberately not a full keyboard, so remappers like
  Toshy/xwaykeyz leave it alone).
- Waits for the mouse and re-attaches automatically after sleep or reconnects.

## Requirements

- GNOME on Wayland (developed on Fedora 45, GNOME 51)
- `python3-libevdev`, `python3-gobject`
- Read access to `/dev/input/event*` and write access to `/dev/uinput`, e.g.
  membership of the `input` group (`sudo usermod -aG input $USER`, then log out
  and in).
- Tested with the Magic Mouse 2 over Bluetooth. The Magic Mouse 1 and USB-C
  model are matched by product ID but untested — reports welcome.

The service file assumes the repo lives at `~/projects/magic-mouse-gestures`;
edit `ExecStart` if you clone it elsewhere.

## Tuning

Constants at the top of `magic_mouse_gestures.py`:

- **Scrolling:** `SCROLL_GAIN` (speed), `SCROLL_SIGN_X/Y` (direction),
  `MOMENTUM_TAU` (how far a flick glides).
- **Gestures:** `SWIPE_MIN_DX` (how far to swipe), `DOUBLE_TAP_WINDOW`.
- **Pointer:** `POINTER_SCALE` slows the pointer below GNOME's minimum speed
  for finer control (1.0 = unchanged). The Magic Mouse tends to feel fast on
  Linux.

Apply changes with `systemctl --user restart magic-mouse-gestures`.
Logs: `journalctl --user -u magic-mouse-gestures -f`. Run in the foreground
with `-v` for debugging.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
