# magic-mouse-gestures

Apple Magic Mouse gestures and smooth momentum scrolling for GNOME on Linux.

The `hid-magicmouse` kernel driver already reports every finger on the Magic
Mouse's touch surface, but libinput treats the device as a plain mouse, GNOME
only supports gestures on touchpads, and the driver's scroll emulation is coarse
wheel clicks with no momentum. This small daemon fills the gap:

| Gesture | Action |
|---|---|
| One-finger scroll | Smooth high-resolution scrolling with momentum |
| Two-finger double-tap | Toggle the Activities overview (like Mission Control) |
| Two-finger swipe left/right | Switch workspace (natural direction, like Spaces) |

## How it works

The daemon grabs the Magic Mouse exclusively and re-emits pointer motion and
buttons through a virtual clone (`<name> (gestures)`). The driver's own wheel
events are dropped and replaced by scrolling computed from the raw touches,
emitted as hi-res wheel events with momentum after a fling.

- Overview: `org.gnome.Shell` D-Bus property `OverviewActive`.
- Workspaces: GNOME's default `Super+Page Up` / `Super+Page Down`, sent from a
  minimal virtual keyboard.

## Requirements

- GNOME on Wayland (tested on Fedora 45, GNOME 51)
- `python3-libevdev`, `python3-gobject`
- Read access to `/dev/input/event*` and write access to `/dev/uinput`
  (e.g. membership of the `input` group). No root needed at runtime.

## Install

```sh
sudo dnf install python3-libevdev python3-gobject
git clone https://github.com/jasperblues/magic-mouse-gestures ~/projects/magic-mouse-gestures
systemctl --user enable --now ~/projects/magic-mouse-gestures/magic-mouse-gestures.service
```

Logs: `journalctl --user -u magic-mouse-gestures -f`. Run in the foreground with
`-v` for debugging.

## Tuning

Constants at the top of `magic_mouse_gestures.py`: swipe distance, tap timing,
scroll gain and direction, momentum strength. Restart the service after editing:
`systemctl --user restart magic-mouse-gestures`.

Pointer speed is GNOME's setting (Settings → Mouse & Touchpad); the Magic Mouse
tends to feel fast at the default.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
