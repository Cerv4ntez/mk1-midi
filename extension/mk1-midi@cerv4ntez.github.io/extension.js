// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) 2026 Cerv4ntez
// Maschine MK1 MIDI - GNOME Shell front end for the mk1-midi daemon.
//
// Adds a top-bar "M" whose dropdown shows and switches the Maschine's mode,
// and a Quick Settings toggle with a menu showing the live pad grid, the active
// group/channel and the last control touched. All state lives in the daemon;
// this talks to it over the session bus (io.github.cerv4ntez.Mk1Midi).

import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import Clutter from 'gi://Clutter';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'io.github.cerv4ntez.Mk1Midi';
const OBJ_PATH = '/io/github/cerv4ntez/Mk1Midi';
const GROUP_NAMES = 'ABCDEFGH';

const IFACE_XML = `
<node>
  <interface name="io.github.cerv4ntez.Mk1Midi1">
    <method name="SetMidiMode"><arg type="b" name="enabled" direction="in"/></method>
    <method name="Toggle"/>
    <method name="SelectGroup"><arg type="i" name="index" direction="in"/></method>
    <method name="Panic"/>
    <property name="Connected" type="b" access="read"/>
    <property name="MidiMode" type="b" access="read"/>
    <property name="Group" type="i" access="read"/>
    <property name="Channel" type="i" access="read"/>
    <property name="PortName" type="s" access="read"/>
    <signal name="Pads"><arg type="ay" name="levels"/></signal>
    <signal name="Control"><arg type="s" name="label"/></signal>
  </interface>
</node>`;
const MaschineProxy = Gio.DBusProxy.makeProxyWrapper(IFACE_XML);

function logCallError(e) {
    console.warn(`mk1-midi: ${e.message}`);
}

/** 4x4 pad grid laid out like the hardware: pad 1 bottom-left. */
const PadGrid = GObject.registerClass(
class PadGrid extends St.Widget {
    _init() {
        const layout = new Clutter.GridLayout({row_spacing: 6, column_spacing: 6});
        super._init({
            style_class: 'maschine-pad-grid',
            layout_manager: layout,
            x_align: Clutter.ActorAlign.CENTER,
        });
        this._pads = [];
        for (let n = 1; n <= 16; n++) {
            const pad = new St.Label({
                style_class: 'maschine-pad',
                text: String(n),
                y_align: Clutter.ActorAlign.CENTER,
            });
            layout.attach(pad, (n - 1) % 4, 3 - Math.floor((n - 1) / 4), 1, 1);
            this._pads.push(pad);
        }
    }

    setLevels(levels) {
        this._pads.forEach((pad, i) => {
            const level = levels[i] ?? 0;
            if (level > 0) {
                const alpha = (0.25 + 0.75 * level / 127).toFixed(2);
                pad.set_style(`background-color: rgba(255, 140, 40, ${alpha});`);
                pad.add_style_pseudo_class('active');
            } else {
                pad.set_style(null);
                pad.remove_style_pseudo_class('active');
            }
        });
    }
});

const MaschineToggle = GObject.registerClass({
    Signals: {'mode-changed': {}},
}, class MaschineToggle extends QuickMenuToggle {
    _init(extension) {
        super._init({
            title: 'MK1 MIDI',
            gicon: extension.icon,
            toggleMode: false,
        });
        this._extension = extension;
        this._proxy = null;
        this._proxySignals = [];

        this.menu.setHeader(extension.icon, 'Maschine MK1', 'MIDI mode');

        // live pads
        const padItem = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        this._padGrid = new PadGrid();
        padItem.add_child(this._padGrid);
        padItem.x_align = Clutter.ActorAlign.CENTER;
        this.menu.addMenuItem(padItem);

        // group selector A-H
        const groupItem = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const groupBox = new St.BoxLayout({style_class: 'maschine-group-box', x_expand: true});
        this._groupButtons = [...GROUP_NAMES].map((name, i) => {
            const button = new St.Button({
                style_class: 'maschine-group button',
                label: name,
                can_focus: true,
                x_expand: true,
            });
            button.connect('clicked', () =>
                this._proxy?.SelectGroupAsync(i).catch(logCallError));
            groupBox.add_child(button);
            return button;
        });
        groupItem.add_child(groupBox);
        this.menu.addMenuItem(groupItem);

        // last control touched / connection status
        const statusItem = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        this._statusLabel = new St.Label({style_class: 'maschine-status', x_expand: true});
        statusItem.add_child(this._statusLabel);
        this.menu.addMenuItem(statusItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('All Notes Off', () =>
            this._proxy?.PanicAsync().catch(logCallError));
        this.menu.addAction('Settings', () => {
            Main.panel.closeQuickSettings?.();
            this._extension.openPreferences();
        });

        this.connect('clicked', () => {
            if (this._proxy)
                this._proxy.ToggleAsync().catch(logCallError);
        });

        this._lastControl = '';
        this._sync();
    }

    setProxy(proxy) {
        this._proxy = proxy;
        this._proxySignals = [
            proxy.connect('g-properties-changed', () => this._sync()),
            proxy.connect('notify::g-name-owner', () => this._sync()),
        ];
        this._padsId = proxy.connectSignal('Pads', (p, sender, [levels]) =>
            this._padGrid.setLevels(levels));
        this._controlId = proxy.connectSignal('Control', (p, sender, [label]) => {
            this._lastControl = label;
            this._syncStatus();
        });
        this._sync();
    }

    get midiMode() {
        return Boolean(this._proxy?.g_name_owner && this._proxy.MidiMode);
    }

    _sync() {
        const proxy = this._proxy;
        const running = Boolean(proxy?.g_name_owner);
        const connected = running && proxy.Connected;

        this.checked = this.midiMode;
        if (!running)
            this.subtitle = 'Daemon not running';
        else if (!connected)
            this.subtitle = 'Not connected';
        else if (this.checked)
            this.subtitle = `Group ${GROUP_NAMES[proxy.Group] ?? '?'} · Ch ${proxy.Channel}`;
        else
            this.subtitle = 'Off';

        this.menu.setHeader(this._extension.icon, 'Maschine MK1',
            running && connected ? `MIDI port “${proxy.PortName}”` : this.subtitle);

        this._groupButtons.forEach((button, i) => {
            if (running && proxy.Group === i)
                button.add_style_pseudo_class('checked');
            else
                button.remove_style_pseudo_class('checked');
            button.reactive = this.checked;
        });
        if (!this.checked)
            this._padGrid.setLevels([]);
        this._syncStatus();
        this.emit('mode-changed');
    }

    _syncStatus() {
        const running = Boolean(this._proxy?.g_name_owner);
        let text;
        if (!running)
            text = 'Start the daemon: systemctl --user start mk1-midi';
        else if (!this._proxy.Connected)
            text = 'Plug in the Maschine MK1';
        else if (!this.checked)
            text = 'Press SHIFT + CONTROL on the Maschine to enter MIDI mode';
        else
            text = this._lastControl || 'Play something…';
        this._statusLabel.text = text;
    }

    destroy() {
        if (this._proxy) {
            this._proxySignals.forEach(id => this._proxy.disconnect(id));
            this._proxy.disconnectSignal(this._padsId);
            this._proxy.disconnectSignal(this._controlId);
            this._proxy = null;
        }
        super.destroy();
    }
});
/** Hosts the Quick Settings toggle; the top-bar icon is MaschineButton. */
const MaschineIndicator = GObject.registerClass(
class MaschineIndicator extends SystemIndicator {
    _init(extension) {
        super._init();
        this._toggle = new MaschineToggle(extension);
        this.quickSettingsItems.push(this._toggle);

        this._lastMode = null;
        this._toggle.connect('mode-changed', () => {
            const on = this._toggle.midiMode;
            if (this._lastMode !== null && on !== this._lastMode)
                showOsd(extension.icon, on ? 'MIDI Mode On' : 'MIDI Mode Off');
            this._lastMode = on;
        });
    }

    setProxy(proxy) {
        this._toggle.setProxy(proxy);
    }

    destroy() {
        this.quickSettingsItems.forEach(item => item.destroy());
        super.destroy();
    }
});

/** Top-bar "M": shows the Maschine's current mode and switches it. */
const MaschineButton = GObject.registerClass(
class MaschineButton extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, 'Maschine MK1', false);
        this._extension = extension;
        this._proxy = null;
        this._proxySignals = [];

        this._icon = new St.Icon({gicon: extension.icon, style_class: 'system-status-icon'});
        this.add_child(this._icon);

        this._status = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this._status.label.add_style_class_name('maschine-menu-status');
        this.menu.addMenuItem(this._status);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Mode'));

        this._modeItems = [
            [true, 'MIDI Mode'],
            [false, 'Off'],
        ].map(([midi, label]) => {
            const item = this.menu.addAction(label, () =>
                this._proxy?.SetMidiModeAsync(midi).catch(logCallError));
            item._midi = midi;
            return item;
        });

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('Settings', () => this._extension.openPreferences());
        this._sync();
    }

    setProxy(proxy) {
        this._proxy = proxy;
        this._proxySignals = [
            proxy.connect('g-properties-changed', () => this._sync()),
            proxy.connect('notify::g-name-owner', () => this._sync()),
        ];
        this._sync();
    }

    _sync() {
        const proxy = this._proxy;
        const running = Boolean(proxy?.g_name_owner);
        const connected = running && proxy.Connected;
        const midi = running && proxy.MidiMode;

        if (!running)
            this._status.label.text = 'Maschine MK1 · daemon not running';
        else if (!connected)
            this._status.label.text = 'Maschine MK1 · not connected';
        else if (midi)
            this._status.label.text = `MIDI Mode · Group ${GROUP_NAMES[proxy.Group] ?? '?'} · Ch ${proxy.Channel}`;
        else
            this._status.label.text = 'Maschine MK1 · Off';

        for (const item of this._modeItems) {
            item.setOrnament(running && item._midi === midi
                ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NO_DOT);
            item.sensitive = connected;
        }
        if (midi)
            this._icon.add_style_class_name('maschine-icon-active');
        else
            this._icon.remove_style_class_name('maschine-icon-active');
        this._icon.opacity = connected ? 255 : 128;
    }

    destroy() {
        if (this._proxy)
            this._proxySignals.forEach(id => this._proxy.disconnect(id));
        this._proxy = null;
        super.destroy();
    }
});

function showOsd(icon, label) {
    const osd = Main.osdWindowManager;
    if (osd.showAll)
        osd.showAll(icon, label, null, null);
    else
        osd.show(-1, icon, label, null, null);
}

export default class Mk1MidiExtension extends Extension {
    enable() {
        this.icon = Gio.icon_new_for_string(
            GLib.build_filenamev([this.path, 'icons', 'mk1-m-symbolic.svg']));
        this._indicator = new MaschineIndicator(this);
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
        this._button = new MaschineButton(this);
        Main.panel.addToStatusArea(this.uuid, this._button);

        this._cancellable = new Gio.Cancellable();
        // Building the proxy without DO_NOT_AUTO_START lets D-Bus activation
        // start the daemon if it is not running yet.
        new MaschineProxy(Gio.DBus.session, BUS_NAME, OBJ_PATH, (proxy, error) => {
            if (error) {
                if (!error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    console.warn(`mk1-midi: ${error.message}`);
                return;
            }
            this._indicator.setProxy(proxy);
            this._button.setProxy(proxy);
        }, this._cancellable, Gio.DBusProxyFlags.NONE);
    }

    disable() {
        this._cancellable?.cancel();
        this._cancellable = null;
        this._button?.destroy();
        this._button = null;
        this._indicator?.destroy();
        this._indicator = null;
        this.icon = null;
    }
}
