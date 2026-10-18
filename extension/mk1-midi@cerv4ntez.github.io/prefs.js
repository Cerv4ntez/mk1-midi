// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) 2026 Cerv4ntez
// Preferences for Maschine MK1 MIDI: edits the daemon's mapping over D-Bus.
// The config lives in ~/.config/mk1-midi/config.json, owned by the daemon.

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

const BUS_NAME = 'io.github.cerv4ntez.Mk1Midi';
const OBJ_PATH = '/io/github/cerv4ntez/Mk1Midi';

const IFACE_XML = `
<node>
  <interface name="io.github.cerv4ntez.Mk1Midi1">
    <method name="GetConfig"><arg type="s" name="json" direction="out"/></method>
    <method name="GetDefaultConfig"><arg type="s" name="json" direction="out"/></method>
    <method name="SetConfig"><arg type="s" name="json" direction="in"/></method>
  </interface>
</node>`;
const MaschineProxy = Gio.DBusProxy.makeProxyWrapper(IFACE_XML);

const BUTTON_SECTIONS = [
    ['Transport', [
        ['restart', 'Restart'], ['step_left', 'Step ◀'], ['step_right', 'Step ▶'],
        ['grid', 'Grid'], ['play', 'Play'], ['rec', 'Rec'], ['erase', 'Erase'],
        ['shift', 'Shift'],
    ]],
    ['Left Column', [
        ['mute', 'Mute'], ['solo', 'Solo'], ['select', 'Select'],
        ['duplicate', 'Duplicate'], ['navigate', 'Navigate'], ['pad_mode', 'Pad Mode'],
        ['pattern', 'Pattern'], ['scene', 'Scene'], ['note_repeat', 'Note Repeat'],
    ]],
    ['Screen Area', [
        ['control', 'Control'], ['step', 'Step'], ['browse', 'Browse'],
        ['sampling', 'Sampling'], ['nav_left', 'Navigate ◀'], ['nav_right', 'Navigate ▶'],
        ['autowrite', 'Auto Write'], ['snap', 'Snap'],
    ]],
    ['Softkeys', Array.from({length: 8}, (_, i) => [`softkey_${i + 1}`, `Softkey ${i + 1}`])],
    ['Groups', [...'ABCDEFGH'].map(c => [`group_${c.toLowerCase()}`, `Group ${c}`])],
];
const ALL_BUTTONS = BUTTON_SECTIONS.flatMap(([, buttons]) => buttons);

const KNOBS = [
    ...Array.from({length: 8}, (_, i) => [`knob_${i + 1}`, `Knob ${i + 1}`]),
    ['volume', 'Volume'], ['tempo', 'Tempo'], ['swing', 'Swing'],
];

const NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
const noteName = n => `${NOTE_NAMES[n % 12]}${Math.floor(n / 12) - 2}`;

export default class Mk1MidiPreferences extends ExtensionPreferences {
    async fillPreferencesWindow(window) {
        window.set_default_size(640, 760);
        window.search_enabled = true;
        this._window = window;
        this._pages = [];
        window.connect('close-request', () => {
            // flush an edit still waiting for its debounce
            if (this._saveId) {
                GLib.source_remove(this._saveId);
                this._saveId = 0;
                this._save();
            }
            return false;
        });

        try {
            this._proxy = await new Promise((resolve, reject) => {
                new MaschineProxy(Gio.DBus.session, BUS_NAME, OBJ_PATH,
                    (proxy, error) => (error ? reject(error) : resolve(proxy)),
                    null, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES);
            });
            const [json] = await this._proxy.GetConfigAsync();
            this._cfg = JSON.parse(json);
        } catch (e) {
            window.add(this._errorPage(e));
            return;
        }
        this._buildPages();
    }

    _buildPages() {
        this._pages.forEach(page => this._window.remove(page));
        this._pages = [
            this._generalPage(),
            this._screensPage(),
            this._padsPage(),
            this._knobsPage(),
            this._buttonsPage(),
        ];
        this._pages.forEach(page => this._window.add(page));
    }

    _errorPage(error) {
        const page = new Adw.PreferencesPage({title: 'MK1 MIDI', icon_name: 'dialog-warning-symbolic'});
        const group = new Adw.PreferencesGroup();
        group.add(new Adw.StatusPage({
            icon_name: 'dialog-warning-symbolic',
            title: 'Daemon Not Reachable',
            description: 'Start it with “systemctl --user start mk1-midi”, ' +
                `then reopen these settings.\n\n${GLib.markup_escape_text(error.message, -1)}`,
        }));
        page.add(group);
        return page;
    }

    // --- persistence

    _changed() {
        if (this._saveId)
            GLib.source_remove(this._saveId);
        this._saveId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 300, () => {
            this._saveId = 0;
            this._save();
            return GLib.SOURCE_REMOVE;
        });
    }

    _save() {
        this._proxy.SetConfigAsync(JSON.stringify(this._cfg)).catch(e =>
            this._window.add_toast(new Adw.Toast({title: `Could not save: ${e.message}`})));
    }

    async _resetDefaults() {
        try {
            const [json] = await this._proxy.GetDefaultConfigAsync();
            this._cfg = JSON.parse(json);
            await this._proxy.SetConfigAsync(json);
            this._buildPages();
            this._window.add_toast(new Adw.Toast({title: 'Defaults restored'}));
        } catch (e) {
            this._window.add_toast(new Adw.Toast({title: `Reset failed: ${e.message}`}));
        }
    }

    // --- row helpers

    _spin(title, subtitle, get, set, lower, upper, step = 1, digits = 0) {
        const row = Adw.SpinRow.new_with_range(lower, upper, step);
        Object.assign(row, {title, subtitle: subtitle ?? '', digits});
        row.value = get();
        row.connect('notify::value', () => {
            set(digits ? row.value : Math.round(row.value));
            this._changed();
        });
        return row;
    }

    /** A slider with a spin button beside it for exact values. */
    _slider(title, subtitle, get, set, lower, upper, step = 1, mark = null) {
        const adjustment = new Gtk.Adjustment({
            lower, upper, step_increment: step, page_increment: step * 16, value: get(),
        });
        const row = new Adw.ActionRow({title, subtitle: subtitle ?? ''});
        const scale = new Gtk.Scale({
            adjustment,
            draw_value: false,
            hexpand: true,
            valign: Gtk.Align.CENTER,
            width_request: 200,
        });
        if (mark !== null)
            scale.add_mark(mark, Gtk.PositionType.BOTTOM, null);
        row.add_suffix(scale);
        row.add_suffix(new Gtk.SpinButton({adjustment, valign: Gtk.Align.CENTER}));
        adjustment.connect('value-changed', () => {
            set(Math.round(adjustment.value));
            this._changed();
        });
        return row;
    }

    _combo(title, subtitle, options, get, set) {
        const row = new Adw.ComboRow({
            title,
            subtitle: subtitle ?? '',
            model: Gtk.StringList.new(options.map(([, label]) => label)),
        });
        row.selected = Math.max(options.findIndex(([id]) => id === get()), 0);
        row.connect('notify::selected', () => {
            set(options[row.selected][0]);
            this._changed();
        });
        return row;
    }

    _switch(title, subtitle, get, set) {
        const row = new Adw.SwitchRow({title, subtitle: subtitle ?? '', active: get()});
        row.connect('notify::active', () => {
            set(row.active);
            this._changed();
        });
        return row;
    }

    /** Free-text names shown on the Maschine's displays; empty = "CC n". */
    _labels(title, items, store) {
        const group = new Adw.PreferencesGroup({
            title,
            description: 'Shown on the Maschine’s screens. Leave empty to show the CC number.',
        });
        for (const [id, label] of items) {
            const row = new Adw.EntryRow({title: label, text: store[id] ?? ''});
            row.connect('changed', () => {
                const text = row.text.trim();
                if (text)
                    store[id] = text;
                else
                    delete store[id];
                this._changed();
            });
            group.add(row);
        }
        return group;
    }

    // --- pages

    _generalPage() {
        const cfg = this._cfg;
        const page = new Adw.PreferencesPage({title: 'General', icon_name: 'preferences-system-symbolic'});

        const mode = new Adw.PreferencesGroup({
            title: 'MIDI Mode',
            description: 'The hotkey toggles MIDI mode from the hardware, like on Windows.',
        });
        const hotkeyOptions = ALL_BUTTONS;
        mode.add(this._combo('Hotkey Modifier', 'Hold this…', hotkeyOptions,
            () => cfg.hotkey[0], v => (cfg.hotkey[0] = v)));
        mode.add(this._combo('Hotkey Button', '…and press this', hotkeyOptions,
            () => cfg.hotkey[1], v => (cfg.hotkey[1] = v)));
        mode.add(this._switch('Start in MIDI Mode', 'Enter MIDI mode as soon as the Maschine is plugged in',
            () => cfg.start_in_midi_mode, v => (cfg.start_in_midi_mode = v)));
        mode.add(this._switch('Hotkey Hint', 'Dimly light the hotkey buttons while MIDI mode is off',
            () => cfg.hint_leds, v => (cfg.hint_leds = v)));
        page.add(mode);

        const channels = new Adw.PreferencesGroup({title: 'Channels'});
        const channelRow = this._spin('MIDI Channel', 'Used when the group buttons send CCs',
            () => cfg.channel, v => (cfg.channel = v), 1, 16);
        channels.add(this._combo('Group Buttons', null, [
            ['channel', 'Select MIDI channel 1–8'],
            ['cc', 'Send CCs'],
        ], () => cfg.group_mode, v => {
            cfg.group_mode = v;
            channelRow.sensitive = v === 'cc';
        }));
        channelRow.sensitive = cfg.group_mode === 'cc';
        channels.add(channelRow);
        page.add(channels);

        const port = new Adw.PreferencesGroup({
            title: 'Port',
            description: 'A new name applies after “systemctl --user restart mk1-midi”.',
        });
        const nameRow = new Adw.EntryRow({title: 'ALSA Client Name', text: cfg.port_name, show_apply_button: true});
        nameRow.connect('apply', () => {
            cfg.port_name = nameRow.text.trim() || 'Maschine MK1';
            this._changed();
        });
        port.add(nameRow);
        page.add(port);

        const reset = new Adw.PreferencesGroup();
        const resetRow = new Adw.ButtonRow({title: 'Reset All Settings to Defaults'});
        resetRow.add_css_class('destructive-action');
        resetRow.connect('activated', () => this._resetDefaults());
        reset.add(resetRow);
        page.add(reset);
        return page;
    }

    _screensPage() {
        const disp = this._cfg.display;
        const page = new Adw.PreferencesPage({title: 'Screens', icon_name: 'video-display-symbolic'});

        const screens = new Adw.PreferencesGroup({
            title: 'Displays',
            description: 'MIDI mode shows softkey names on top, knob values at the bottom ' +
                'and the last control in the middle. Otherwise the date and time.',
        });
        screens.add(this._switch('Use the Displays', null,
            () => disp.enabled, v => (disp.enabled = v)));
        screens.add(this._switch('Date and Time', 'Date on the left, time on the right while ' +
            'MIDI mode is off or idle (otherwise the hotkey hint)',
            () => disp.clock, v => (disp.clock = v)));
        screens.add(this._spin('Clock After', 'Minutes without touching the Maschine in MIDI mode (0 = never)',
            () => disp.idle_minutes, v => (disp.idle_minutes = v), 0, 60));
        page.add(screens);

        const contrast = new Adw.PreferencesGroup({
            title: 'Contrast',
            description: 'Panel voltage, 0–511; changes apply live. The mark is the default (253).',
        });
        contrast.add(this._slider('Left Screen', null,
            () => disp.contrast[0], v => (disp.contrast[0] = v), 0, 511, 1, 253));
        contrast.add(this._slider('Right Screen', null,
            () => disp.contrast[1], v => (disp.contrast[1] = v), 0, 511, 1, 253));
        page.add(contrast);

        const light = new Adw.PreferencesGroup({title: 'Backlight'});
        light.add(this._spin('In MIDI Mode', '0–63',
            () => disp.backlight, v => (disp.backlight = v), 0, 63));
        light.add(this._spin('While Off', '0–63',
            () => disp.idle_backlight, v => (disp.idle_backlight = v), 0, 63));
        page.add(light);
        return page;
    }

    _padsPage() {
        const pads = this._cfg.pads;
        const page = new Adw.PreferencesPage({title: 'Pads', icon_name: 'view-grid-symbolic'});

        const notes = new Adw.PreferencesGroup({title: 'Notes'});
        const base = this._spin('Pad 1 Note', noteName(pads.base_note),
            () => pads.base_note, v => {
                pads.base_note = v;
                base.subtitle = `${noteName(v)} – pad 16 plays ${noteName(Math.min(v + 15, 127))}`;
            }, 0, 112);
        base.subtitle = `${noteName(pads.base_note)} – pad 16 plays ${noteName(Math.min(pads.base_note + 15, 127))}`;
        notes.add(base);
        notes.add(this._spin('Idle LED Brightness', 'Pad lights in MIDI mode while not hit (0 = off, 63 = full)',
            () => pads.idle_brightness, v => (pads.idle_brightness = v), 0, 63));
        notes.add(this._combo('Aftertouch', 'Pressure while a pad is held', [
            ['off', 'Off'], ['poly', 'Polyphonic'], ['channel', 'Channel'],
        ], () => pads.aftertouch, v => (pads.aftertouch = v)));
        page.add(notes);

        const velocity = new Adw.PreferencesGroup({title: 'Velocity'});
        const fixed = this._spin('Fixed Velocity', null,
            () => pads.fixed_velocity, v => (pads.fixed_velocity = v), 1, 127);
        velocity.add(this._combo('Curve', null, [
            ['soft', 'Soft'], ['linear', 'Linear'], ['hard', 'Hard'], ['fixed', 'Fixed'],
        ], () => pads.velocity_curve, v => {
            pads.velocity_curve = v;
            fixed.sensitive = v === 'fixed';
        }));
        fixed.sensitive = pads.velocity_curve === 'fixed';
        velocity.add(fixed);
        velocity.add(this._spin('Full-Scale Pressure', 'Pressure that gives velocity 127 (max 4095)',
            () => pads.full_scale, v => (pads.full_scale = v), 500, 4095, 50));
        page.add(velocity);

        const sensing = new Adw.PreferencesGroup({title: 'Sensing'});
        sensing.add(this._spin('Trigger Threshold', 'Pressure that starts a note',
            () => pads.threshold, v => (pads.threshold = v), 20, 2000, 10));
        sensing.add(this._spin('Release Threshold', 'Pressure below which the note ends',
            () => pads.release, v => (pads.release = v), 5, 1000, 5));
        sensing.add(this._spin('Velocity Window', 'Milliseconds spent finding the hit’s peak',
            () => pads.velocity_window_ms, v => (pads.velocity_window_ms = v), 1, 30));
        page.add(sensing);
        return page;
    }

    _knobsPage() {
        const knobs = this._cfg.knobs;
        const page = new Adw.PreferencesPage({title: 'Knobs', icon_name: 'preferences-other-symbolic'});

        const behaviour = new Adw.PreferencesGroup({
            title: 'Behaviour',
            description: 'The MK1 knobs are endless encoders.',
        });
        behaviour.add(this._combo('Mode', null, [
            ['absolute', 'Absolute (0–127)'],
            ['relative', 'Relative (64 ± steps)'],
        ], () => knobs.mode, v => (knobs.mode = v)));
        behaviour.add(this._spin('Turns for Full Range', 'How far to turn for 0 → 127',
            () => knobs.turns, v => (knobs.turns = v), 0.25, 5, 0.25, 2));
        page.add(behaviour);

        const ccs = new Adw.PreferencesGroup({title: 'Control Change Numbers'});
        for (const [id, label] of KNOBS) {
            ccs.add(this._spin(label, null,
                () => knobs.cc[id], v => (knobs.cc[id] = v), 0, 127));
        }
        page.add(ccs);
        page.add(this._labels('Names', KNOBS.slice(0, 8), knobs.labels));
        return page;
    }

    _buttonsPage() {
        const buttons = this._cfg.buttons;
        const page = new Adw.PreferencesPage({title: 'Buttons', icon_name: 'input-gaming-symbolic'});

        for (const [section, list] of BUTTON_SECTIONS) {
            const group = new Adw.PreferencesGroup({
                title: section,
                description: section === 'Groups' && this._cfg.group_mode === 'channel'
                    ? 'Groups currently select the MIDI channel; these CCs are used in “Send CCs” mode.'
                    : '',
            });
            for (const [id, label] of list) {
                const row = this._spin(label, null,
                    () => buttons.cc[id], v => (buttons.cc[id] = v), 0, 127);
                const latch = new Gtk.CheckButton({
                    label: 'Latch',
                    active: buttons.toggle.includes(id),
                    valign: Gtk.Align.CENTER,
                    tooltip_text: 'Toggle on/off with each press instead of momentary',
                });
                latch.connect('toggled', () => {
                    buttons.toggle = buttons.toggle.filter(b => b !== id);
                    if (latch.active)
                        buttons.toggle.push(id);
                    this._changed();
                });
                row.add_prefix(latch);
                group.add(row);
            }
            page.add(group);
        }
        page.add(this._labels('Softkey Names', BUTTON_SECTIONS[3][1], buttons.labels));
        return page;
    }
}
