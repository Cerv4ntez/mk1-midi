# mk1-midi

**An unofficial MIDI mode for the Native Instruments Maschine MK1 on Linux**,
with a GNOME Shell extension as the front end.

> Not affiliated with or endorsed by Native Instruments. Maschine and Native
> Instruments are trademarks of Native Instruments GmbH, used here only to name
> the hardware this project works with.

The original Maschine controller (2009, USB `17cc:0808`) is no longer supported
by its maker. mk1-midi turns it into a general-purpose MIDI controller on Linux,
using its pressure-sensitive pads, endless knobs, all buttons, LEDs and both
displays.

## Features

* **MIDI mode** toggled from the hardware (**SHIFT + CONTROL**), the top bar or
  Quick Settings.
* Pads send notes with velocity (and optional aftertouch), knobs and buttons
  send CCs, and the group buttons A–H select MIDI channel 1–8.
* **Both displays:** softkey names, knob values and the last control touched in
  MIDI mode; date and time otherwise.
* **LED feedback** from your DAW, and the MK1's own DIN MIDI sockets bridged to
  ALSA.
* An idle mode that turns all lights off after two minutes untouched.
* Everything is mappable in a GNOME preferences window.

## How it works

On Windows, the MK1's MIDI mode is implemented by NI's host software, not by
the controller itself. mk1-midi does the same job on Linux:

```
Maschine MK1 ──USB (pyusb, mk1usb.py)──▶ mk1_midi.py (daemon)
   pads, knobs, buttons  ──▶                ├─ "Maschine MK1:MIDI Out"         ──▶ DAW
   LEDs, 2 × 255×64 screens ◀──             ├─ "Maschine MK1 Feedback"         ◀── DAW (LEDs)
   DIN MIDI In/Out  ◀──▶                    ├─ "Maschine MK1 DIN In" / "DIN Out"  (the MK1's sockets)
                                            └─ D-Bus io.github.cerv4ntez.Mk1Midi ◀──▶ GNOME extension
```

The kernel's snd-usb-caiaq driver can read the controller but cannot drive the
displays, so the daemon takes the device over while it runs and hands it back
to the kernel driver when it stops.

## Requirements

* Linux with GNOME Shell 50–52 (the daemon also works without GNOME; the
  extension is the only UI)
* Python 3.10+ with pyusb, python-rtmidi, Pillow and PyGObject:

  | Distribution | Packages |
  |---|---|
  | Arch / CachyOS | `python-pyusb python-rtmidi python-pillow python-gobject` |
  | Debian / Ubuntu | `python3-usb python3-rtmidi python3-pil python3-gi` |
  | Fedora | `python3-pyusb python3-rtmidi python3-pillow python3-gobject` |

  If your distribution lacks one of them, `pip install --user pyusb
  python-rtmidi pillow` works too (PyGObject is best taken from the distribution).

## Install

```bash
git clone https://github.com/Cerv4ntez/mk1-midi.git
cd mk1-midi
./install.sh            # asks for sudo once, for the udev rule
./install.sh --no-udev  # skip the udev rule
```

This installs the daemon to `~/.local/share/mk1-midi`, a systemd user service
(`mk1-midi.service`) and the extension. **Log out and back in once** afterwards:
GNOME on Wayland only discovers new extensions at login.

The udev rule (`system/70-maschine-mk1.rules`) gives your login session access
to the MK1's USB device. It also hides the controller from libinput while the
kernel driver owns it: the kernel reports the MK1's buttons with codes that
overlap mouse buttons, so without the rule **Note Repeat is a left click and
Group A a right click** whenever the daemon isn't running.

`./uninstall.sh` removes everything except your mapping in
`~/.config/mk1-midi/config.json`.

## Use

* **SHIFT + CONTROL** on the MK1 toggles MIDI mode. While it is off, Shift and
  Control glow dimly as a hint. In MIDI mode the pads glow at the lowest
  brightness while idle.
* The **M** in the top bar shows the mode: accent-colored in MIDI mode, faded
  when the controller isn't connected. Click it for the current mode,
  *MIDI Mode* / *Off*, and *Settings*.
* The **MK1 MIDI** toggle in Quick Settings switches too. Its menu shows the pads
  live, the active group and the last control touched, and has
  *All Notes Off*.
* Connect **Maschine MK1 → MIDI Out** to your DAW or synth (PipeWire, Ardour,
  Bitwig, `aconnect`, …).

### Screens

| | Left screen | Right screen | Lights |
|---|---|---|---|
| MIDI mode | softkey 1–4 names · group + channel · knob 1–4 names, values, bars | softkey 5–8 names · last control touched + value bar · knob 5–8 | backlight on, pads glow |
| MIDI mode, untouched for 2 min | weekday + date | time (HH:MM) | all off |
| Off | weekday + date | time (HH:MM) | backlight off |

The columns line up with the softkeys above and the knobs below each screen.
Name knobs and softkeys in *Settings → Knobs / Buttons* (otherwise "CC n").
*Settings → Screens* has contrast (per screen), backlight levels, the date/time screens and
the idle delay. The date follows your `LC_TIME` locale.

The input that wakes the controller from idle only wakes it: that button press,
pad hit or knob turn (plus half a second of turning) sends no MIDI.

### Default mapping

| Control | MIDI |
|---|---|
| Pads 1–16 | Notes 36–51 (C1–D#2), velocity from pressure |
| Groups A–H | Select MIDI channel 1–8 (or send CC 52–59) |
| Knobs 1–8 | CC 14–21, absolute 0–127 (endless knobs, 1 turn = full range) |
| Volume / Tempo / Swing | CC 7 / 22 / 23 |
| Mute, Solo, Select, Duplicate, Navigate, Pad Mode, Pattern, Scene | CC 24–31 |
| Softkeys 1–8 | CC 102–109 |
| Restart, Step ◀, Step ▶, Grid, Play, Rec, Erase, Shift | CC 110–117 |
| Control, Step, Browse, Sampling, Nav ◀, Nav ▶ | CC 85–90 |
| Auto Write, Snap, Note Repeat | CC 118, 119, 9 |

Everything is editable in the extension's Settings: velocity curve, thresholds,
aftertouch, relative knobs, latching buttons, the hotkey and more.

**LED feedback:** notes sent *to* "Maschine MK1 Feedback" light the matching
pad, and CCs light the matching button.

## Troubleshooting

* Daemon log: `journalctl --user -u mk1-midi -f`. Run it by hand with
  `MK1_MIDI_DEBUG=1 python3 daemon/mk1_midi.py` to log every button.
* Only one program can drive the MK1 over USB. Stop the daemon
  (`systemctl --user stop mk1-midi`) before using other libusb tools.
* "no reply to GET_DEVICE_INFO": the controller's firmware is stuck. Re-plug it.
* Screens lit but empty: raise that screen's *Contrast* slider in Settings → Screens. The
  displays' usable contrast varies between units; the default (253) was set by eye on one
  unit.
* A knob that has been resting must turn about 11° before it counts. This
  filters out pots that drift on their own (one test unit's Swing knob wanders
  by ~2 % of a turn).

## Files

| Path | What |
|---|---|
| `daemon/mk1_midi.py` | the daemon: input → MIDI, LEDs, screens, DIN bridge, D-Bus |
| `daemon/mk1usb.py` | userspace USB driver: commands, input reports, pads, displays |
| `daemon/screens.py` | display layouts (Pillow → packed 5-bit frames) |
| `extension/mk1-midi@cerv4ntez.github.io/` | GNOME Shell extension and preferences |
| `system/70-maschine-mk1.rules` | udev rule: USB access, hide the MK1 from libinput |
| `install.sh`, `uninstall.sh` | per-user install |

## Credits

mk1-midi builds on the work of others:

* The Linux kernel's **snd-usb-caiaq** driver (`sound/usb/caiaq`) by Daniel Mack
  and contributors (GPL-2.0-or-later). The knob decoding in `daemon/mk1usb.py`
  is a port of its `decode_erp()`, and the button, LED and knob layout tables
  follow its tables.
* **[cabl](https://github.com/shaduzlabs/cabl)** by Vincenzo Pacella (MIT): the
  display initialization sequence and frame format.
* **[maschine-mk1](https://github.com/BlueJayLouche/maschine-mk1)** by
  BlueJayLouche (MIT or Apache-2.0): its USB protocol notes were the starting
  point for the userspace driver.

## License

mk1-midi is free software, licensed under the **GNU General Public License
v2.0 or later**. See [LICENSE](LICENSE).
