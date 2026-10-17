# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Cerv4ntez
"""Renders the two 255x64 MK1 displays with Pillow.

Layout follows the hardware: softkeys 1-4 sit above the left screen and 5-8
above the right one, knobs 1-4 / 5-8 below them. So each screen is split into
four columns: softkey labels on top, knob bars at the bottom, status between.
Pixels are drawn directly in the panels' 0-31 gray range.
"""
import subprocess
import time

from PIL import Image, ImageDraw, ImageFont

import mk1usb

W, H = mk1usb.WIDTH, mk1usb.HEIGHT
BRIGHT, MID, DIM, FAINT = 31, 20, 10, 5


def _font_path(pattern):
    try:
        return subprocess.run(['fc-match', '-f', '%{file}', pattern],
                              capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _font(pattern, size):
    path = _font_path(pattern)
    try:
        return ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
    except OSError:
        return ImageFont.load_default(size)


class Screens:
    def __init__(self):
        self.small = _font('DejaVu Sans:condensed', 9)
        self.small_bold = _font('DejaVu Sans:condensed:bold', 9)
        self.medium = _font('DejaVu Sans:bold', 13)
        self.large = _font('DejaVu Sans:bold', 22)
        self.clock = _font('DejaVu Sans:light', 30)
        self.weekday = _font('DejaVu Sans:bold', 16)
        self.date = _font('DejaVu Sans', 24)
        self.time = _font('DejaVu Sans', 58)

    # --- helpers

    @staticmethod
    def _column(i):
        return round(i * W / 4), round((i + 1) * W / 4) - 1

    def _fit(self, d, text, font, width):
        """Ellipsize text to the pixel width."""
        if d.textlength(text, font=font) <= width:
            return text
        while text and d.textlength(text + '…', font=font) > width:
            text = text[:-1]
        return text + '…'

    def _centered(self, d, x0, x1, y, text, font, fill):
        text = self._fit(d, text, font, x1 - x0 - 2)
        x = x0 + (x1 - x0 + 1 - d.textlength(text, font=font)) / 2
        d.text((x, y), text, font=font, fill=fill)

    def _softkeys(self, d, keys):
        for i, (label, active) in enumerate(keys):
            x0, x1 = self._column(i)
            if active:
                d.rounded_rectangle((x0 + 1, 0, x1 - 1, 10), radius=2, fill=BRIGHT)
                self._centered(d, x0, x1, 0, label, self.small_bold, 0)
            else:
                self._centered(d, x0, x1, 0, label, self.small, MID)
        d.line((0, 12, W - 1, 12), fill=FAINT)

    def _knobs(self, d, knobs):
        d.line((0, 39, W - 1, 39), fill=FAINT)
        for i, (label, value, active) in enumerate(knobs):
            x0, x1 = self._column(i)
            font = self.small_bold if active else self.small
            ink = 0 if active else MID
            if active:
                d.rounded_rectangle((x0 + 1, 42, x1 - 1, 53), radius=2, fill=BRIGHT)
            text = '–' if value is None else str(value)
            tw = d.textlength(text, font=font)
            d.text((x1 - 3 - tw, 42), text, font=font, fill=ink)
            d.text((x0 + 3, 42), self._fit(d, label, font, x1 - x0 - tw - 9),
                   font=font, fill=ink)
            bx0, bx1 = x0 + 3, x1 - 3
            d.rectangle((bx0, 57, bx1, 61), outline=DIM)
            if value:
                d.rectangle((bx0, 57, bx0 + round((bx1 - bx0) * value / 127), 61),
                            fill=BRIGHT if active else MID)

    @staticmethod
    def _image():
        img = Image.new('L', (W, H), 0)
        return img, ImageDraw.Draw(img)

    @staticmethod
    def _pack(img):
        return mk1usb.pack_frame(img.tobytes())

    # --- screens

    def render(self, s):
        """s: state dict from the daemon. Returns (left_frame, right_frame)."""
        if s['show_clock']:
            return self._clock()
        if not s['midi_mode']:
            return self._idle(s)

        left, dl = self._image()
        right, dr = self._image()
        self._softkeys(dl, s['softkeys'][:4])
        self._softkeys(dr, s['softkeys'][4:])
        self._knobs(dl, s['knobs'][:4])
        self._knobs(dr, s['knobs'][4:])

        # left centre: group badge + channel
        dl.rounded_rectangle((2, 15, 27, 36), radius=3, fill=BRIGHT)
        self._centered(dl, 2, 27, 13, s['group'], self.large, 0)
        dl.text((34, 14), f"Channel {s['channel']}", font=self.medium, fill=BRIGHT)
        dl.text((34, 28), self._fit(dl, s['port'], self.small, W - 36),
                font=self.small, fill=DIM)

        # right centre: last event + value bar
        event, value = s['event'], s['event_value']
        dr.text((3, 14), self._fit(dr, event or 'MIDI mode', self.medium, W - 6),
                font=self.medium, fill=BRIGHT if event else DIM)
        if value is not None:
            dr.rectangle((3, 31, W - 4, 35), outline=DIM)
            dr.rectangle((3, 31, 3 + round((W - 7) * value), 35), fill=BRIGHT)
        return self._pack(left), self._pack(right)

    def _clock(self):
        """Date on the left screen, HH:MM on the right (locale's LC_TIME)."""
        left, dl = self._image()
        right, dr = self._image()
        now = time.localtime()
        self._centered(dl, 0, W - 1, 6, time.strftime('%A', now), self.weekday, MID)
        self._centered(dl, 0, W - 1, 28, f"{now.tm_mday}. {time.strftime('%B %Y', now)}",
                       self.date, BRIGHT)
        # anchor 'mm' centres the digits' bounding box, not the font's line box
        dr.text((W / 2, H / 2), time.strftime('%H:%M', now), font=self.time,
                fill=BRIGHT, anchor='mm')
        return self._pack(left), self._pack(right)

    def _idle(self, s):
        left, dl = self._image()
        right, dr = self._image()
        dl.text((6, 4), 'MK1 MIDI', font=self.large, fill=BRIGHT)
        dl.text((8, 32), 'MIDI mode off', font=self.medium, fill=MID)
        dl.text((8, 50), self._fit(dl, f"port: {s['port']}", self.small, W - 10),
                font=self.small, fill=DIM)

        if s['clock']:
            self._centered(dr, 0, W - 1, 2, time.strftime('%H:%M'), self.clock, MID)
        hotkey = ' + '.join(k.replace('_', ' ').upper() for k in s['hotkey'])
        self._centered(dr, 0, W - 1, 40, hotkey, self.small_bold, BRIGHT)
        self._centered(dr, 0, W - 1, 51, 'enters MIDI mode', self.small, MID)
        return self._pack(left), self._pack(right)
