#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Cerv4ntez
"""mk1-midi: a Linux "MIDI mode" for the Native Instruments Maschine MK1.

On Windows the MK1's MIDI mode is not a hardware mode at all: NI's host agent
reads the controller and emits MIDI on a virtual port. This daemon does the
same on Linux, talking to the controller directly over USB (mk1usb):

  * input    - pads, knobs and buttons
  * output   - LEDs and the two 255x64 displays (screens.py)
  * MIDI     - ALSA sequencer virtual ports via python-rtmidi: controller out,
               LED feedback in, and a bridge for the MK1's DIN MIDI In/Out
  * control  - session bus service io.github.cerv4ntez.Mk1Midi (GNOME extension UI)

While it runs, the kernel's snd-usb-caiaq driver is detached; it gets the
device back when the daemon stops. The hotkey (default SHIFT + CONTROL)
toggles MIDI mode, like on Windows.
"""
import copy
import json
import locale
import os
import signal
import sys
import time
from pathlib import Path

import rtmidi
import usb.core
from gi.repository import Gio, GLib
try:
    from gi.repository import GLibUnix
    unix_signal_add = GLibUnix.signal_add
except ImportError:     # GLib < 2.80
    unix_signal_add = GLib.unix_signal_add

import mk1usb
from screens import Screens

VERSION = '0.3.0'
BUS_NAME = 'io.github.cerv4ntez.Mk1Midi'
OBJ_PATH = '/io/github/cerv4ntez/Mk1Midi'
IFACE = 'io.github.cerv4ntez.Mk1Midi1'

CONFIG_PATH = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') \
    / 'mk1-midi' / 'config.json'
DEBUG = bool(os.environ.get('MK1_MIDI_DEBUG'))

GROUPS = [f'group_{c}' for c in 'abcdefgh']
KNOBS = [f'knob_{i}' for i in range(1, 9)]
SOFTKEYS = [f'softkey_{i}' for i in range(1, 9)]
KNOB_RANGE = 1000           # decode_erp() output is 0..999, wrapping
KNOB_DEADBAND = 10          # step size while a knob is being turned
KNOB_UNLOCK = 30            # a resting knob must move this far to count: the
                            # swing knob toggles across ~20 units untouched
KNOB_SETTLE = 1.0           # seconds without movement before a knob rests again
WAKE_GRACE = 0.5            # seconds after waking from idle in which knobs send nothing
PAD_DEADBAND = 6            # pressure change worth forwarding from the USB thread
LED_MAX = 63


def printed_pad(raw):
    """Raw pad ids run row-major from the top-left; the printed numbers start
    bottom-left (raw 12 = pad 1, raw 3 = pad 16 - verified on hardware)."""
    return (3 - raw // 4) * 4 + raw % 4 + 1


# --- configuration -----------------------------------------------------------

DEFAULT_CONFIG = {
    'port_name': 'Maschine MK1',
    'hotkey': ['shift', 'control'],
    'start_in_midi_mode': False,
    'hint_leds': True,          # dimly light the hotkey while MIDI mode is off
    'group_mode': 'channel',    # 'channel': groups A-H select MIDI ch 1-8; 'cc'
    'channel': 1,               # MIDI channel when group_mode is 'cc'
    'display': {
        'enabled': True,
        'contrast': [mk1usb.DEFAULT_CONTRAST] * 2,  # left, right: panel electronic volume 0-511
        'backlight': 63,        # 0-63 in MIDI mode
        'idle_backlight': 0,    # 0-63 while MIDI mode is off
        'clock': True,          # date (left) + time (right) when off or idle
        'idle_minutes': 2,      # MIDI mode: show the clock after this long untouched (0 = never)
    },
    'pads': {
        'base_note': 36,        # pad 1 = C1 (GM kick)
        'threshold': 150,       # pressure that triggers a note (0-4095)
        'release': 60,          # pressure below which the note ends
        'full_scale': 3000,     # pressure that gives velocity 127
        'velocity_curve': 'linear',     # soft | linear | hard | fixed
        'fixed_velocity': 100,
        'velocity_window_ms': 5,        # peak search after the first touch
        'aftertouch': 'off',            # off | poly | channel
        'idle_brightness': 1,           # pad LEDs while not hit, in MIDI mode (0-63)
    },
    'knobs': {
        'mode': 'absolute',     # absolute (virtual 0-127) | relative (64 +/- n)
        'turns': 1.0,           # knob turns for the full 0-127 range
        'cc': {
            'knob_1': 14, 'knob_2': 15, 'knob_3': 16, 'knob_4': 17,
            'knob_5': 18, 'knob_6': 19, 'knob_7': 20, 'knob_8': 21,
            'volume': 7, 'tempo': 22, 'swing': 23,
        },
        'labels': {},           # knob name -> text shown on the displays
    },
    'buttons': {
        'cc': {
            'mute': 24, 'solo': 25, 'select': 26, 'duplicate': 27,
            'navigate': 28, 'pad_mode': 29, 'pattern': 30, 'scene': 31,
            'control': 85, 'step': 86, 'browse': 87, 'sampling': 88,
            'nav_left': 89, 'nav_right': 90, 'autowrite': 118, 'snap': 119,
            **{f'softkey_{i}': 101 + i for i in range(1, 9)},
            'restart': 110, 'step_left': 111, 'step_right': 112, 'grid': 113,
            'play': 114, 'rec': 115, 'erase': 116, 'shift': 117,
            'note_repeat': 9,
            **{g: 52 + i for i, g in enumerate(GROUPS)},
        },
        'toggle': [],           # buttons that latch on/off instead of momentary
        'labels': {},           # softkey name -> text shown on the displays
    },
}

CURVES = {'soft': 0.5, 'linear': 1.0, 'hard': 2.0}


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def merge_config(override):
    cfg = deep_merge(DEFAULT_CONFIG, override)
    contrast = cfg['display']['contrast']
    if not isinstance(contrast, list):      # older configs had one value for both
        cfg['display']['contrast'] = [contrast] * 2
    return cfg


def load_config():
    try:
        return merge_config(json.loads(CONFIG_PATH.read_text()))
    except FileNotFoundError:
        return copy.deepcopy(DEFAULT_CONFIG)
    except (OSError, ValueError) as e:
        log(f'config unreadable ({e}), using defaults')
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(cfg, indent=2) + '\n')
    tmp.replace(CONFIG_PATH)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class MidiParser:
    """Reassembles complete MIDI messages from the DIN byte stream."""
    LENGTHS = {0x80: 3, 0x90: 3, 0xA0: 3, 0xB0: 3, 0xC0: 2, 0xD0: 2, 0xE0: 3,
               0xF1: 2, 0xF2: 3, 0xF3: 2, 0xF6: 1}

    def __init__(self):
        self.status = None
        self.buf = []
        self.sysex = None

    def feed(self, data):
        out = []
        for b in data:
            if b >= 0xF8:                           # realtime, may interleave
                out.append([b])
            elif b == 0xF0:
                self.sysex = [b]
            elif self.sysex is not None:
                self.sysex.append(b)
                if b == 0xF7:
                    out.append(self.sysex)
                    self.sysex = None
            elif b & 0x80:
                self.status = b
                self.buf = [b]
                if self.LENGTHS.get(b, 0) == 1:
                    out.append(self.buf)
                    self.status = None
            elif self.status is not None:
                if not self.buf:
                    self.buf = [self.status]        # running status
                self.buf.append(b)
                if len(self.buf) == self.LENGTHS.get(self.status & 0xF0 if self.status < 0xF0
                                                     else self.status, 3):
                    out.append(self.buf)
                    self.buf = []
                    if self.status >= 0xF0:
                        self.status = None
        return out


# --- the daemon ---------------------------------------------------------------

DBUS_XML = f'''
<node>
  <interface name="{IFACE}">
    <method name="SetMidiMode"><arg type="b" name="enabled" direction="in"/></method>
    <method name="Toggle"/>
    <method name="SelectGroup"><arg type="i" name="index" direction="in"/></method>
    <method name="Panic"/>
    <method name="GetConfig"><arg type="s" name="json" direction="out"/></method>
    <method name="GetDefaultConfig"><arg type="s" name="json" direction="out"/></method>
    <method name="SetConfig"><arg type="s" name="json" direction="in"/></method>
    <property name="Connected" type="b" access="read"/>
    <property name="MidiMode" type="b" access="read"/>
    <property name="Group" type="i" access="read"/>
    <property name="Channel" type="i" access="read"/>
    <property name="PortName" type="s" access="read"/>
    <property name="Version" type="s" access="read"/>
    <signal name="Pads"><arg type="ay" name="levels"/></signal>
    <signal name="Control"><arg type="s" name="label"/></signal>
  </interface>
</node>'''


class Pad:
    __slots__ = ('state', 'peak', 'note', 'channel', 'timer', 'aftertouch', 'level')

    def __init__(self):
        self.state = 'idle'     # idle | arming | on | ignored (hit that woke the MK1)
        self.peak = 0
        self.note = 0
        self.channel = 0
        self.timer = 0
        self.aftertouch = -1
        self.level = 0          # 0-127, for the UI


class Daemon:
    def __init__(self):
        self.cfg = load_config()
        self.loop = GLib.MainLoop()
        self.conn = None
        self.dev = None
        self.rescan_id = 0
        self.connected = False
        self.midi_mode = False
        self.group = 0
        self.buttons = 0            # last READ_IO bitfield
        self.held = set()           # buttons physically held
        self.swallowed = set()      # held buttons whose release must not send
        self.pads = {n: Pad() for n in range(1, 17)}
        self.pad_raw = [0] * 16     # USB thread only
        self.knob_anchor = {}
        self.knob_motion = {}       # knob -> (direction, monotonic time of last move)
        self.knob_value = {}        # (group, knob) -> float 0-127
        self.latched = {}           # (group, button) -> bool (toggle buttons)
        self.remote_leds = {}       # (group, 'pad_n' | button) -> 0-63 from MIDI in
        self.led_flush_id = 0
        self.pads_signal_id = 0
        self.control_label = None
        self.control_signal_id = 0

        self.screens = Screens()
        self.screen_id = 0
        self.sent_frames = [None, None]
        self.event_text = ''
        self.event_value = None
        self.recent_knob = None
        self.recent_knob_id = 0
        self.last_activity = time.monotonic()
        self.screensaver = False    # MIDI mode, idle: showing the clock
        self.wake_grace = 0.0       # monotonic time until which knobs stay silent

        name = self.cfg['port_name']
        self.midi_out = rtmidi.MidiOut(rtmidi.API_LINUX_ALSA, name=name)
        self.midi_out.open_virtual_port('MIDI Out')
        self.midi_in = rtmidi.MidiIn(rtmidi.API_LINUX_ALSA, name=name + ' Feedback')
        self.midi_in.open_virtual_port('LED Feedback In')
        self.midi_in.set_callback(self._on_midi_in)
        # the MK1's own DIN sockets
        self.din_in = rtmidi.MidiOut(rtmidi.API_LINUX_ALSA, name=name + ' DIN In')
        self.din_in.open_virtual_port('DIN In')
        self.din_out = rtmidi.MidiIn(rtmidi.API_LINUX_ALSA, name=name + ' DIN Out')
        self.din_out.ignore_types(sysex=False, timing=False, active_sense=False)
        self.din_out.open_virtual_port('DIN Out')
        self.din_out.set_callback(self._on_din_out)
        self.din_parser = MidiParser()

    # --- lifecycle

    def run(self):
        Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                         self._on_bus_acquired, None,
                         lambda *a: (log('could not own bus name'), self.loop.quit()))
        for sig in (signal.SIGINT, signal.SIGTERM):
            unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self.quit)
        GLib.timeout_add_seconds(5, self._tick_clock)
        if not self._try_connect():
            self._schedule_rescan()
        self.loop.run()

    def quit(self):
        self.set_midi_mode(False)
        self._disconnect(reattach=True)
        for port in (self.midi_out, self.midi_in, self.din_in, self.din_out):
            port.close_port()
        self.loop.quit()
        return GLib.SOURCE_REMOVE

    # --- device handling

    def _try_connect(self):
        if not mk1usb.Mk1Usb.present():
            return False
        dev = mk1usb.Mk1Usb(on_buttons=self._usb_buttons, on_knobs=self._usb_knobs,
                            on_pads=self._usb_pads, on_midi=self._usb_midi,
                            on_lost=self._usb_lost)
        dev.contrast = [clamp(int(c), 0, 511) for c in self.cfg['display']['contrast']]
        try:
            dev.open()
        except (OSError, usb.core.USBError) as e:
            log(f'cannot open Maschine MK1: {e}')
            return False
        self.dev = dev
        self.connected = True
        self.buttons = 0
        self.pad_raw = [0] * 16
        self.knob_anchor.clear()
        self.knob_motion.clear()
        self.sent_frames = [None, None]
        log('connected')
        self._notify('Connected')
        if self.cfg['start_in_midi_mode'] and not self.midi_mode:
            self.set_midi_mode(True)
        self._refresh_leds()
        self._screen_dirty()
        return True

    def _disconnect(self, reattach):
        if not self.connected:
            return
        self._release_all()
        if self.dev:
            self.dev.close(reattach=reattach)
            self.dev = None
        self.held.clear()
        self.swallowed.clear()
        self.connected = False
        log('disconnected')
        self._notify('Connected')

    def _schedule_rescan(self):
        if not self.rescan_id:
            self.rescan_id = GLib.timeout_add_seconds(2, self._rescan)

    def _rescan(self):
        if self._try_connect():
            self.rescan_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    # --- USB thread callbacks (marshalled onto the main loop)

    def _usb_buttons(self, bits):
        GLib.idle_add(self._on_buttons, bits)

    def _usb_knobs(self, values):
        GLib.idle_add(self._on_knobs, values)

    def _usb_pads(self, pressures):
        # ~700 reports/s: only forward real changes
        changed = {}
        for raw, p in pressures.items():
            old = self.pad_raw[raw]
            if abs(p - old) >= PAD_DEADBAND or (p == 0) != (old == 0):
                self.pad_raw[raw] = p
                changed[printed_pad(raw)] = p
        if changed:
            GLib.idle_add(self._on_pads, changed)

    def _usb_midi(self, data):
        GLib.idle_add(self._on_din_in, bytes(data))

    def _usb_lost(self):
        GLib.idle_add(self._on_lost)

    def _on_lost(self):
        log('device lost')
        self._disconnect(reattach=False)
        self._schedule_rescan()
        return GLib.SOURCE_REMOVE

    # --- input

    def _on_buttons(self, bits):
        changed = bits ^ self.buttons
        self.buttons = bits
        for i, name in enumerate(mk1usb.BUTTON_BITS):
            if name and changed >> i & 1:
                self._on_button(name, bool(bits >> i & 1))
        return GLib.SOURCE_REMOVE

    def _on_knobs(self, values):
        for name, raw in values.items():
            self._on_knob(name, raw)
        return GLib.SOURCE_REMOVE

    def _on_pads(self, changed):
        if self.midi_mode:
            for n, pressure in changed.items():
                self._on_pad(n, pressure)
        return GLib.SOURCE_REMOVE

    def _on_button(self, name, down):
        if DEBUG:
            log(f'button {name} {"down" if down else "up"} held={sorted(self.held)}')
        if self._activity() and down:
            self.held.add(name)
            self.swallowed.add(name)    # the waking press only wakes
            return
        if down:
            self.held.add(name)
            hotkey = self.cfg['hotkey']
            if name in hotkey and all(b in self.held for b in hotkey):
                # the hotkey's buttons never leak MIDI for this press
                self.swallowed.update(hotkey)
                self.set_midi_mode(not self.midi_mode)
                return
        else:
            self.held.discard(name)
            if name in self.swallowed:
                self.swallowed.discard(name)
                self._refresh_leds()
                return
        if not self.midi_mode:
            self._refresh_leds()
            return

        if name in GROUPS and self.cfg['group_mode'] == 'channel':
            if down:
                self.select_group(GROUPS.index(name))
            return

        cc = self.cfg['buttons']['cc'].get(name)
        if name in self.cfg['buttons']['toggle']:
            if not down:
                return
            key = (self.group, name)
            self.latched[key] = not self.latched.get(key, False)
            value = 127 if self.latched[key] else 0
        else:
            value = 127 if down else 0
        if cc is not None:
            self._send([0xB0 | self.channel, cc, value])
            label = self.cfg['buttons']['labels'].get(name) or pretty(name)
            self._report(f'{label} · CC {cc} = {value}', value / 127)
        self._refresh_leds()
        if name in SOFTKEYS:
            self._screen_dirty()

    def _on_knob(self, name, raw):
        prev = self.knob_anchor.get(name)
        if prev is None:
            self.knob_anchor[name] = raw
            return
        delta = (raw - prev + KNOB_RANGE // 2) % KNOB_RANGE - KNOB_RANGE // 2
        if abs(delta) < KNOB_DEADBAND:
            return
        # Noise flips back and forth; a real turn keeps its direction. A
        # resting knob, or one reversing, needs KNOB_UNLOCK of travel.
        now = time.monotonic()
        direction, last = self.knob_motion.get(name, (0, 0.0))
        turning = now - last < KNOB_SETTLE and direction == (1 if delta > 0 else -1)
        if not turning and abs(delta) < KNOB_UNLOCK:
            return
        self.knob_motion[name] = (1 if delta > 0 else -1, now)
        self.knob_anchor[name] = raw
        if self._activity() or time.monotonic() < self.wake_grace:
            return                      # the waking turn only wakes
        if not self.midi_mode:
            return
        cc = self.cfg['knobs']['cc'].get(name)
        if cc is None:
            return
        label = self.cfg['knobs']['labels'].get(name) or pretty(name)
        self._touch_knob(name)
        steps = delta * 128 / (KNOB_RANGE * max(self.cfg['knobs']['turns'], 0.05))
        if self.cfg['knobs']['mode'] == 'relative':
            n = clamp(round(steps) or (1 if delta > 0 else -1), -63, 63)
            self._send([0xB0 | self.channel, cc, 64 + n])
            self._report(f'{label} · CC {cc} {n:+d}', None)
            return
        key = (self.group, name)
        old = self.knob_value.get(key, 0.0)
        new = clamp(old + steps, 0.0, 127.0)
        self.knob_value[key] = new
        if int(new) != int(old):
            self._send([0xB0 | self.channel, cc, int(new)])
            self._report(f'{label} · CC {cc} = {int(new)}', int(new) / 127)

    def _touch_knob(self, name):
        self.recent_knob = name
        if self.recent_knob_id:
            GLib.source_remove(self.recent_knob_id)
        self.recent_knob_id = GLib.timeout_add(1500, self._untouch_knob)

    def _untouch_knob(self):
        self.recent_knob = None
        self.recent_knob_id = 0
        self._screen_dirty()
        return GLib.SOURCE_REMOVE

    def _on_pad(self, n, pressure):
        p = self.pads[n]
        pc = self.cfg['pads']
        if p.state == 'idle':
            if pressure >= pc['threshold']:
                if self._activity():
                    p.state = 'ignored'     # the waking hit only wakes
                    return
                p.state = 'arming'
                p.peak = pressure
                p.timer = GLib.timeout_add(max(int(pc['velocity_window_ms']), 1),
                                           self._pad_fire, n)
        elif p.state == 'ignored':
            if pressure < pc['release']:
                p.state = 'idle'
        elif p.state == 'arming':
            p.peak = max(p.peak, pressure)
            if pressure < pc['release']:
                GLib.source_remove(p.timer)
                p.timer = 0
                self._pad_fire(n)
                self._pad_off(n)
        elif pressure < pc['release']:
            self._pad_off(n)
        elif pc['aftertouch'] != 'off':
            at = int(clamp((pressure - pc['release']) * 127
                           / max(pc['full_scale'] - pc['release'], 1), 0, 127))
            if at != p.aftertouch:
                p.aftertouch = at
                if pc['aftertouch'] == 'poly':
                    self._send([0xA0 | p.channel, p.note, at])
                else:
                    self._send([0xD0 | p.channel, at])
                self._set_pad_level(n, max(at, 1))

    def _pad_fire(self, n):
        p = self.pads[n]
        p.timer = 0
        if p.state != 'arming':
            return GLib.SOURCE_REMOVE
        pc = self.cfg['pads']
        if pc['velocity_curve'] == 'fixed':
            vel = pc['fixed_velocity']
        else:
            span = max(pc['full_scale'] - pc['threshold'], 1)
            x = clamp((p.peak - pc['threshold']) / span, 0.0, 1.0)
            vel = round(127 * x ** CURVES.get(pc['velocity_curve'], 1.0))
        vel = int(clamp(vel, 1, 127))
        p.state = 'on'
        p.note = clamp(pc['base_note'] + n - 1, 0, 127)
        p.channel = self.channel
        p.aftertouch = -1
        self._send([0x90 | p.channel, p.note, vel])
        self._report(f'Pad {n} · {note_name(p.note)} vel {vel}', vel / 127)
        self._set_pad_level(n, vel)
        return GLib.SOURCE_REMOVE

    def _pad_off(self, n):
        p = self.pads[n]
        if p.state == 'arming' and p.timer:
            GLib.source_remove(p.timer)
            p.timer = 0
        if p.state == 'on':
            self._send([0x80 | p.channel, p.note, 0])
        p.state = 'idle'
        self._set_pad_level(n, 0)

    def _set_pad_level(self, n, level):
        self.pads[n].level = level
        self._refresh_pad_led(n)
        if not self.pads_signal_id:
            self.pads_signal_id = GLib.timeout_add(40, self._emit_pads)

    # --- MIDI

    @property
    def channel(self):
        if self.cfg['group_mode'] == 'channel':
            return self.group
        return clamp(int(self.cfg['channel']), 1, 16) - 1

    def _send(self, msg):
        try:
            self.midi_out.send_message(msg)
        except rtmidi.RtMidiError as e:
            log(f'MIDI send failed: {e}')

    def _on_midi_in(self, event, data=None):
        msg, _ = event
        GLib.idle_add(self._handle_feedback, list(msg))

    def _handle_feedback(self, msg):
        """Notes on a pad's note light the pad, CCs on a button's CC light the
        button - so a DAW can drive the LEDs like with NI's MIDI templates."""
        if len(msg) < 3:
            return GLib.SOURCE_REMOVE
        kind, ch = msg[0] & 0xF0, msg[0] & 0x0F
        group = ch if self.cfg['group_mode'] == 'channel' else self.group
        if self.cfg['group_mode'] != 'channel' and ch != self.channel:
            return GLib.SOURCE_REMOVE
        if kind in (0x80, 0x90):
            n = msg[1] - self.cfg['pads']['base_note'] + 1
            if 1 <= n <= 16:
                on = kind == 0x90 and msg[2] > 0
                self.remote_leds[(group, f'pad_{n}')] = msg[2] // 2 if on else 0
                self._refresh_pad_led(n)
        elif kind == 0xB0:
            for name, cc in self.cfg['buttons']['cc'].items():
                if cc == msg[1]:
                    self.remote_leds[(group, name)] = msg[2] // 2
            self._refresh_leds()
            self._screen_dirty()
        return GLib.SOURCE_REMOVE

    def _on_din_in(self, data):
        for msg in self.din_parser.feed(data):
            try:
                self.din_in.send_message(msg)
            except rtmidi.RtMidiError as e:
                log(f'DIN in forward failed: {e}')
        return GLib.SOURCE_REMOVE

    def _on_din_out(self, event, data=None):
        msg, _ = event
        dev = self.dev
        if dev and self.connected:
            try:
                dev.midi_write(bytes(msg))
            except usb.core.USBError as e:
                log(f'DIN out failed: {e}')

    def _release_all(self):
        """Note-off every sounding pad and release held momentary buttons."""
        for n, p in self.pads.items():
            if p.state != 'idle':
                self._pad_off(n)
        if self.midi_mode:
            for name in self.held - self.swallowed:
                cc = self.cfg['buttons']['cc'].get(name)
                if cc is not None and name not in self.cfg['buttons']['toggle'] \
                        and not (name in GROUPS and self.cfg['group_mode'] == 'channel'):
                    self._send([0xB0 | self.channel, cc, 0])

    def panic(self):
        self._release_all()
        for ch in range(16):
            self._send([0xB0 | ch, 123, 0])     # all notes off
        self._report('All notes off', None)

    # --- modes

    def set_midi_mode(self, on):
        on = bool(on)
        if on == self.midi_mode:
            return
        if not on:
            self._release_all()
        self.midi_mode = on
        self.event_text, self.event_value = '', None
        self.screensaver = False
        self.last_activity = time.monotonic()
        log(f'MIDI mode {"on" if on else "off"}')
        self._refresh_leds()
        self._screen_dirty()
        self._notify('MidiMode')

    def select_group(self, index):
        index = clamp(int(index), 0, 7)
        if index == self.group:
            return
        self._release_all()
        self.group = index
        self._report(f'Group {"ABCDEFGH"[index]} · channel {self.channel + 1}', None)
        self._refresh_leds()
        self._notify('Group', 'Channel')

    def apply_config(self, cfg):
        old = self.cfg
        self._release_all()
        self.cfg = cfg
        if cfg['port_name'] != old['port_name']:
            log('port name changes apply after a daemon restart')
        if self.dev:
            for n, (new, prev) in enumerate(zip(cfg['display']['contrast'],
                                                old['display']['contrast'])):
                if new != prev:
                    self.dev.set_contrast(n, new)
        self.sent_frames = [None, None]
        self._refresh_leds()
        self._screen_dirty()
        self._notify('Channel')

    # --- LEDs

    def _led(self, name, value):
        if not self.dev:
            return
        self.dev.set_led(name, value)
        if not self.led_flush_id:
            self.led_flush_id = GLib.timeout_add(10, self._flush_leds)

    def _flush_leds(self):
        self.led_flush_id = 0
        if self.dev:
            try:
                self.dev.flush_leds()
            except usb.core.USBError as e:
                log(f'LED write failed: {e}')
        return GLib.SOURCE_REMOVE

    def _refresh_pad_led(self, n):
        level = 0
        if self.midi_mode and not self.screensaver:
            level = max(int(self.cfg['pads']['idle_brightness']),
                        self.pads[n].level // 2,
                        self.remote_leds.get((self.group, f'pad_{n}'), 0))
        self._led(f'pad_{n}', level)

    def _button_active(self, name):
        if name in self.cfg['buttons']['toggle']:
            on = self.latched.get((self.group, name), False)
        else:
            on = name in self.held
        return on or self.remote_leds.get((self.group, name), 0) > 0

    def _refresh_leds(self):
        if not self.dev:
            return
        on = self.midi_mode and not self.screensaver   # idle in MIDI mode: all dark
        disp = self.cfg['display']
        backlight = 0
        if disp['enabled'] and not self.screensaver:
            backlight = disp['backlight'] if on else disp['idle_backlight']
        self._led('backlight', backlight)
        for n in self.pads:
            self._refresh_pad_led(n)
        for name in mk1usb.LED_POS:
            if name.startswith('pad_') or name == 'backlight':
                continue
            level = 0
            if on:
                if name in GROUPS and self.cfg['group_mode'] == 'channel':
                    level = LED_MAX if GROUPS.index(name) == self.group else 6
                elif name in self.cfg['buttons']['toggle']:
                    level = LED_MAX if self.latched.get((self.group, name)) else 0
                elif name in self.held:
                    level = LED_MAX
                level = max(level, self.remote_leds.get((self.group, name), 0))
            elif not self.midi_mode and self.cfg['hint_leds'] and name in self.cfg['hotkey']:
                level = LED_MAX if name in self.held else 8
            self._led(name, level)

    # --- displays

    def _screen_dirty(self):
        if self.dev and not self.screen_id:
            self.screen_id = GLib.timeout_add(40, self._render_screens)

    def _activity(self):
        """Note user input. Returns True if it woke the MK1 from idle; that
        input then only wakes it and sends no MIDI."""
        self.last_activity = time.monotonic()
        if not self.screensaver:
            return False
        self.screensaver = False
        self.wake_grace = self.last_activity + WAKE_GRACE
        self._refresh_leds()
        self._screen_dirty()
        return True

    def _tick_clock(self):
        """Every 5 s: enter the idle clock, and keep a shown clock current
        (unchanged frames are not resent)."""
        idle = self.cfg['display']['idle_minutes']
        if self.midi_mode and not self.screensaver and idle > 0 \
                and time.monotonic() - self.last_activity >= idle * 60:
            self.screensaver = True
            self._refresh_leds()
        if not self.midi_mode or self.screensaver:
            self._screen_dirty()
        return GLib.SOURCE_CONTINUE

    def _screen_state(self):
        cfg = self.cfg
        labels = cfg['buttons']['labels']
        softkeys = []
        for name in SOFTKEYS:
            cc = cfg['buttons']['cc'].get(name)
            softkeys.append((labels.get(name) or (f'CC {cc}' if cc is not None else ''),
                             self._button_active(name)))
        knobs = []
        for name in KNOBS:
            cc = cfg['knobs']['cc'].get(name)
            value = None
            if cfg['knobs']['mode'] == 'absolute' and (self.group, name) in self.knob_value:
                value = int(self.knob_value[(self.group, name)])
            knobs.append((cfg['knobs']['labels'].get(name) or (f'CC {cc}' if cc is not None else ''),
                          value, name == self.recent_knob))
        return {
            'midi_mode': self.midi_mode,
            'show_clock': cfg['display']['clock'] and (not self.midi_mode or self.screensaver),
            'connected': self.connected,
            'port': cfg['port_name'],
            'clock': cfg['display']['clock'],
            'hotkey': cfg['hotkey'],
            'group': 'ABCDEFGH'[self.group],
            'channel': self.channel + 1,
            'softkeys': softkeys,
            'knobs': knobs,
            'event': self.event_text,
            'event_value': self.event_value,
        }

    def _render_screens(self):
        self.screen_id = 0
        if not self.dev:
            return GLib.SOURCE_REMOVE
        if self.cfg['display']['enabled']:
            frames = self.screens.render(self._screen_state())
        else:
            frames = (mk1usb.pack_frame(bytes(mk1usb.WIDTH * mk1usb.HEIGHT)),) * 2
        for n, frame in enumerate(frames):
            if frame != self.sent_frames[n]:
                self.sent_frames[n] = frame
                self.dev.show(n, frame)
        return GLib.SOURCE_REMOVE

    # --- D-Bus

    def _on_bus_acquired(self, conn, name):
        self.conn = conn
        node = Gio.DBusNodeInfo.new_for_xml(DBUS_XML)
        conn.register_object(OBJ_PATH, node.interfaces[0],
                             self._dbus_call, self._dbus_get, None)

    def _dbus_get(self, conn, sender, path, iface, prop):
        return {
            'Connected': lambda: GLib.Variant('b', self.connected),
            'MidiMode': lambda: GLib.Variant('b', self.midi_mode),
            'Group': lambda: GLib.Variant('i', self.group),
            'Channel': lambda: GLib.Variant('i', self.channel + 1),
            'PortName': lambda: GLib.Variant('s', self.cfg['port_name']),
            'Version': lambda: GLib.Variant('s', VERSION),
        }[prop]()

    def _dbus_call(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        result = None
        try:
            if method == 'SetMidiMode':
                self.set_midi_mode(args[0])
            elif method == 'Toggle':
                self.set_midi_mode(not self.midi_mode)
            elif method == 'SelectGroup':
                self.select_group(args[0])
            elif method == 'Panic':
                self.panic()
            elif method == 'GetConfig':
                result = GLib.Variant('(s)', (json.dumps(self.cfg),))
            elif method == 'GetDefaultConfig':
                result = GLib.Variant('(s)', (json.dumps(DEFAULT_CONFIG),))
            elif method == 'SetConfig':
                cfg = merge_config(json.loads(args[0]))
                save_config(cfg)
                self.apply_config(cfg)
        except Exception as e:
            invocation.return_dbus_error(f'{IFACE}.Error', str(e))
            return
        invocation.return_value(result)

    def _notify(self, *props):
        if not self.conn:
            return
        changed = {p: self._dbus_get(None, None, None, None, p) for p in props}
        self.conn.emit_signal(None, OBJ_PATH, 'org.freedesktop.DBus.Properties',
                              'PropertiesChanged',
                              GLib.Variant('(sa{sv}as)', (IFACE, changed, [])))

    def _emit_pads(self):
        self.pads_signal_id = 0
        if self.conn:
            levels = bytes(self.pads[n].level for n in range(1, 17))
            self.conn.emit_signal(None, OBJ_PATH, IFACE, 'Pads',
                                  GLib.Variant('(ay)', (levels,)))
        return GLib.SOURCE_REMOVE

    def _report(self, label, value):
        """Last control touched: shown on the right display and in the GNOME menu."""
        self.event_text, self.event_value = label, value
        self._screen_dirty()
        self.control_label = label
        if not self.control_signal_id:
            self.control_signal_id = GLib.timeout_add(50, self._emit_control)

    def _emit_control(self):
        self.control_signal_id = 0
        if self.conn and self.control_label:
            self.conn.emit_signal(None, OBJ_PATH, IFACE, 'Control',
                                  GLib.Variant('(s)', (self.control_label,)))
        return GLib.SOURCE_REMOVE


NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def note_name(n):
    return f'{NOTE_NAMES[n % 12]}{n // 12 - 2}'


def pretty(name):
    return name.replace('_', ' ').title()


if __name__ == '__main__':
    locale.setlocale(locale.LC_TIME, '')    # weekday/month names in the user's language
    Daemon().run()
