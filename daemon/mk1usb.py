# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Cerv4ntez
"""Userspace USB driver for the Maschine MK1 (17cc:0808), via pyusb.

Replaces the kernel's snd-usb-caiaq driver so the daemon can also reach the
two 255x64 displays on EP 0x08, which the kernel driver has no API for.
Protocol facts come from the Linux kernel's sound/usb/caiaq driver, cabl
(github.com/shaduzlabs/cabl) and the protocol notes of
github.com/BlueJayLouche/maschine-mk1, verified on hardware. decode_erp() is
a port of decode_erp() in the kernel's sound/usb/caiaq/input.c,
Copyright (c) 2006,2007 Daniel Mack, Tim Ruetz (GPL-2.0-or-later).
DISPLAY_INIT follows cabl by Vincenzo Pacella (MIT).

Threads: one reader per IN endpoint (EP 0x81 replies/reports, EP 0x84 pads)
and one writer for the displays. Callbacks run on the reader threads; the
caller marshals them to its own main loop.
"""
import threading
import time

import usb.core
import usb.util

VID, PID = 0x17cc, 0x0808
EP_CMD, EP_REPLY, EP_PADS, EP_DISP = 0x01, 0x81, 0x84, 0x08

CMD_DEVICE_INFO = 0x01
CMD_READ_ERP = 0x02
CMD_READ_IO = 0x04
CMD_MIDI_READ = 0x06
CMD_MIDI_WRITE = 0x07
CMD_AUTO_MSG = 0x0b
CMD_DIMM_LEDS = 0x0c

# READ_IO bit i -> button name
BUTTON_BITS = [
    'mute', 'solo', 'select', 'duplicate', 'navigate', 'pad_mode', 'pattern', 'scene',
    None, 'rec', 'erase', 'shift', 'grid', 'step_right', 'step_left', 'restart',
    'group_e', 'group_f', 'group_g', 'group_h', 'group_d', 'group_c', 'group_b', 'group_a',
    'control', 'browse', 'nav_left', 'snap', 'autowrite', 'nav_right', 'sampling', 'step',
    *[f'softkey_{8 - i}' for i in range(8)],
    'note_repeat', 'play',
]

# READ_ERP payload offsets (a, b) per knob
KNOB_TAPERS = {
    'knob_1': (21, 20), 'knob_2': (15, 14), 'knob_3': (9, 8), 'knob_4': (3, 2),
    'knob_5': (19, 18), 'knob_6': (13, 12), 'knob_7': (7, 6), 'knob_8': (1, 0),
    'volume': (17, 16), 'tempo': (11, 10), 'swing': (5, 4),
}

# LED positions in the 64-byte DIMM_LEDS state (kernel control.c)
LED_POS = {
    **{f'pad_{n}': p for n, p in zip(range(1, 17),
                                     (3, 2, 1, 0, 7, 6, 5, 4, 11, 10, 9, 8, 15, 14, 13, 12))},
    'mute': 16, 'solo': 17, 'select': 18, 'duplicate': 19, 'navigate': 20,
    'pad_mode': 21, 'pattern': 22, 'scene': 23, 'shift': 24, 'erase': 25,
    'grid': 26, 'step_right': 27, 'rec': 28, 'play': 29, 'step_left': 32,
    'restart': 33, 'group_a': 41, 'group_b': 40, 'group_c': 37, 'group_d': 36,
    'group_e': 39, 'group_f': 38, 'group_g': 35, 'group_h': 34,
    'autowrite': 42, 'snap': 43, 'nav_right': 44, 'nav_left': 45,
    'sampling': 46, 'browse': 47, 'step': 48, 'control': 49,
    **{f'softkey_{i}': 58 - i for i in range(1, 9)},
    'note_repeat': 58, 'backlight': 59,
}

# --- displays

WIDTH, HEIGHT = 255, 64
ROW_BYTES = 170
FRAME_BYTES = ROW_BYTES * HEIGHT        # 10880
CHUNK, LAST_CHUNK = 502, 338

DISPLAY_INIT = [
    (b'\x30', 0), (b'\xCA\x04\x0F\x00', 20), (b'\xBB\x00', 0), (b'\xD1', 0),
    (b'\x94', 0), (b'\x81\x1E\x02', 20), (b'\x20\x08', 20), (b'\x20\x0B', 20),
    (b'\xA6', 0), (b'\x31', 0), (b'\x32\x00\x00\x05', 0), (b'\x34', 0),
    (b'\x30', 0), (b'\xBC\x00\x01\x02', 0), (b'\x75\x00\x3F', 0),
    (b'\x15\x00\x54', 0), (b'\x5C', 0), (b'\x25', 20), (b'\xAF', 20),
    (b'\xBC\x02\x01\x01', 0), (b'\xA6', 0),
]
# The panels' controller looks like a Sitronix ST7529 (32 gray levels, 3 px in
# 2 bytes). cabl ends the init with electronic volume 0x81 25 02 (= 165), which
# leaves this unit's panels blank; 253 was picked by eye on real hardware.
DEFAULT_CONTRAST = 253


def pack_frame(gray):
    """gray: 255*64 bytes, 0 = dark .. 31 = fully lit -> panel framebuffer.

    3 pixels per 2 bytes, 5 bits each, stored inverted:
    [p0:7..3 | p1:2..0] [p1:7..6 | x | p2:4..0]."""
    out = bytearray(b'\xff' * FRAME_BYTES)
    for y in range(HEIGHT):
        row = gray[y * WIDTH:(y + 1) * WIDTH]
        o = y * ROW_BYTES
        for x in range(0, WIDTH, 3):
            a = 31 - row[x]
            b = 31 - row[x + 1]
            c = 31 - row[x + 2]
            out[o] = (a << 3) | (b >> 2)
            out[o + 1] = ((b & 3) << 6) | 0x20 | c
            o += 2
    return bytes(out)


LOW_PEAK, HIGH_PEAK = -7, 268


def decode_erp(a, b):
    """Two 90-degree shifted tapers -> 0..999 (port of the kernel's decode_erp)."""
    rng = HIGH_PEAK - LOW_PEAK
    deg90, deg180 = rng // 2, rng
    deg270, deg360 = deg90 + deg180, deg180 * 2
    mid = (HIGH_PEAK + LOW_PEAK) // 2
    weight_b = min(max(abs(mid - a) - (rng // 2 - 100) // 2, 0), 100)
    weight_a = 100 - weight_b
    if a < mid:
        pos_b = b - LOW_PEAK + deg270
        if pos_b >= deg360:
            pos_b -= deg360
    else:
        pos_b = HIGH_PEAK - b + deg90
    if b > mid:
        pos_a = a - LOW_PEAK
    else:
        pos_a = HIGH_PEAK - a + deg180
    ret = int((pos_a * weight_a + pos_b * weight_b) * 10 / deg360)
    return ret % 1000


class Mk1Usb:
    def __init__(self, on_buttons=None, on_knobs=None, on_pads=None, on_midi=None,
                 on_lost=None):
        self.on_buttons = on_buttons or (lambda bits: None)
        self.on_knobs = on_knobs or (lambda values: None)
        self.on_pads = on_pads or (lambda pressures: None)
        self.on_midi = on_midi or (lambda data: None)
        self.on_lost = on_lost or (lambda: None)
        self.dev = None
        self.running = False
        self.cmd_lock = threading.Lock()
        self.info_event = threading.Event()
        self.leds = bytearray(64)
        self._sent_leds = [None, None]
        self._threads = []
        self._disp_cond = threading.Condition()
        self._disp_pending = [None, None]
        self._disp_ready = [False, False]
        self.contrast = [DEFAULT_CONTRAST, DEFAULT_CONTRAST]
        self._contrast_pending = [False, False]

    @staticmethod
    def present():
        return usb.core.find(idVendor=VID, idProduct=PID) is not None

    def open(self):
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise OSError('Maschine MK1 not found')
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
        if dev.get_active_configuration().bConfigurationValue != 1:
            dev.set_configuration(1)
        usb.util.claim_interface(dev, 0)
        # Taken over mid-session (e.g. from snd-usb-caiaq) the firmware stops
        # answering EP1 until reset. Resetting while we hold the claim keeps
        # the kernel from re-probing in between.
        dev.reset()
        dev.set_interface_altsetting(0, 1)      # alt 1 exposes EP 0x84 / 0x08
        self.dev = dev
        self.running = True
        for target, name in ((self._read_replies, 'mk1-ep81'),
                             (self._read_pads, 'mk1-ep84'),
                             (self._display_worker, 'mk1-disp')):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        self.command(bytes([CMD_DEVICE_INFO]))
        if not self.info_event.wait(2.0):
            self.close()
            raise OSError('no reply to GET_DEVICE_INFO - power-cycle the MK1')
        self.command(bytes([CMD_AUTO_MSG, 1, 10, 5]))   # report rates as the kernel
        self._sent_leds = [None, None]
        self.flush_leds()

    def close(self, reattach=True):
        """Stop the threads; with reattach (device still present) also blank
        the LEDs and displays and hand the device back to snd-usb-caiaq."""
        if not self.dev:
            return
        self.running = False
        with self._disp_cond:
            self._disp_cond.notify_all()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=1.5)
        self._threads.clear()
        if reattach:
            try:
                self.leds[:] = bytes(64)
                self._sent_leds = [None, None]
                self.flush_leds()
                blank = pack_frame(bytes(WIDTH * HEIGHT))
                for n in (0, 1):
                    if self._disp_ready[n]:
                        self._send_frame(n, blank)
            except usb.core.USBError:
                pass
        dev, self.dev = self.dev, None
        try:
            usb.util.release_interface(dev, 0)
            if reattach:
                # snd-usb-caiaq's probe fails on a mid-session firmware;
                # reset first, then hand the device back.
                dev.reset()
                time.sleep(0.3)
                dev.attach_kernel_driver(0)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(dev)

    # --- EP1 commands

    def command(self, data):
        with self.cmd_lock:
            self.dev.write(EP_CMD, data, timeout=1000)

    def set_led(self, name, value):
        pos = LED_POS.get(name)
        if pos is not None:
            self.leds[pos] = max(0, min(63, int(value)))

    def flush_leds(self):
        """Send whichever 32-byte LED bank changed."""
        if not self.dev:
            return
        for bank, (offset, tag) in enumerate(((0, 0x00), (32, 0x1e))):
            state = bytes(self.leds[offset:offset + 32])
            if state != self._sent_leds[bank]:
                self.command(bytes([CMD_DIMM_LEDS, tag]) + state)
                self._sent_leds[bank] = state

    def midi_write(self, data):
        for i in range(0, len(data), 32):
            part = data[i:i + 32]
            self.command(bytes([CMD_MIDI_WRITE, 0, len(part)]) + part)

    # --- displays

    def show(self, display, frame):
        """Queue a packed frame (FRAME_BYTES) for display 0 (left) or 1 (right).
        Only the newest pending frame per display is sent."""
        with self._disp_cond:
            self._disp_pending[display] = frame
            self._disp_cond.notify()

    def set_contrast(self, display, value):
        """Panel electronic volume, 0-511, for display 0 (left) or 1 (right)."""
        with self._disp_cond:
            self.contrast[display] = max(0, min(511, int(value)))
            self._contrast_pending[display] = True
            self._disp_cond.notify()

    def _display_worker(self):
        while self.running:
            with self._disp_cond:
                while self.running and not any(self._contrast_pending) \
                        and not any(f is not None for f in self._disp_pending):
                    self._disp_cond.wait()
                if not self.running:
                    return
                contrast = [c if p else None
                            for c, p in zip(self.contrast, self._contrast_pending)]
                self._contrast_pending = [False, False]
                n = 0 if self._disp_pending[0] is not None else 1
                frame, self._disp_pending[n] = self._disp_pending[n], None
            try:
                for i in (0, 1):
                    if contrast[i] is not None and self._disp_ready[i]:
                        self._send_contrast(i, contrast[i])
                if frame is not None:
                    if not self._disp_ready[n]:
                        self._init_display(n)
                        self._disp_ready[n] = True
                    self._send_frame(n, frame)
            except usb.core.USBError:
                if self.running:
                    self._lost()
                return

    def _send_contrast(self, n, value):
        self.dev.write(EP_DISP, bytes([n << 1, 0x00, 0x03, 0x81, value & 0x3F, value >> 6]),
                       timeout=1000)

    def _init_display(self, n):
        d = n << 1
        for cmd, delay in DISPLAY_INIT:
            self.dev.write(EP_DISP, bytes([d, 0x00, len(cmd)]) + cmd, timeout=1000)
            if delay:
                time.sleep(delay / 1000)
        self._send_contrast(n, self.contrast[n])

    def _send_frame(self, n, frame):
        d = n << 1
        w = self.dev.write
        w(EP_DISP, bytes([d, 0x00, 0x03, 0x75, 0x00, 0x3F]), timeout=1000)
        w(EP_DISP, bytes([d, 0x00, 0x03, 0x15, 0x00, 0x54]), timeout=1000)
        w(EP_DISP, bytes([d, 0x01, 0xF7, 0x5C]) + frame[:CHUNK], timeout=1000)
        off = CHUNK
        while off + CHUNK <= FRAME_BYTES - LAST_CHUNK:
            w(EP_DISP, bytes([d | 1, 0x01, 0xF6]) + frame[off:off + CHUNK], timeout=1000)
            off += CHUNK
        w(EP_DISP, bytes([d | 1, 0x01, 0x52]) + frame[off:], timeout=1000)

    # --- readers

    def _lost(self):
        if self.running:
            self.running = False
            self.on_lost()

    def _read_replies(self):
        while self.running:
            try:
                data = bytes(self.dev.read(EP_REPLY, 64, timeout=200))
            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError:
                self._lost()
                return
            if not data:
                continue
            cmd, payload = data[0], data[1:]
            if cmd == CMD_READ_IO and len(payload) >= 6:
                self.on_buttons(int.from_bytes(payload[:6], 'little'))
            elif cmd == CMD_READ_ERP and len(payload) >= 22:
                self.on_knobs({k: decode_erp(payload[a], payload[b])
                               for k, (a, b) in KNOB_TAPERS.items()})
            elif cmd == CMD_MIDI_READ and len(payload) >= 2:
                self.on_midi(payload[2:2 + payload[1]])
            elif cmd == CMD_DEVICE_INFO:
                self.info_event.set()

    def _read_pads(self):
        while self.running:
            try:
                data = bytes(self.dev.read(EP_PADS, 512, timeout=200))
            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError:
                self._lost()
                return
            if len(data) < 32:
                continue
            pressures = {}
            for i in range(0, 32, 2):
                word = data[i] | (data[i + 1] << 8)
                pressures[word >> 12] = word & 0xfff
            self.on_pads(pressures)
