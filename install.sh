#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Cerv4ntez
# Installs mk1-midi for the current user:
#   daemon      -> ~/.local/share/mk1-midi/
#   service     -> systemd user unit + D-Bus activation file
#   extension   -> ~/.local/share/gnome-shell/extensions/mk1-midi@cerv4ntez.github.io
#   udev rule   -> /etc/udev/rules.d (asks for sudo; skip with --no-udev)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UUID=mk1-midi@cerv4ntez.github.io
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
PY=/usr/bin/python3
INSTALL_UDEV=1
[[ "${1:-}" == "--no-udev" ]] && INSTALL_UDEV=0

echo "==> checking Python modules"
if ! "$PY" -c 'import usb.core, rtmidi, PIL, gi; gi.require_version("Gio", "2.0")' 2>/dev/null; then
    echo "missing Python modules (pyusb, python-rtmidi, Pillow, PyGObject); see README.md, Requirements" >&2
    exit 1
fi

echo "==> daemon"
install -Dm755 "$HERE/daemon/mk1_midi.py" "$DATA/mk1-midi/mk1_midi.py"
install -Dm644 "$HERE/daemon/mk1usb.py" "$DATA/mk1-midi/mk1usb.py"
install -Dm644 "$HERE/daemon/screens.py" "$DATA/mk1-midi/screens.py"

echo "==> systemd user service + D-Bus activation"
mkdir -p "$CONF/systemd/user" "$DATA/dbus-1/services"
cat > "$CONF/systemd/user/mk1-midi.service" <<EOF
[Unit]
Description=Maschine MK1 MIDI mode

[Service]
Type=dbus
BusName=io.github.cerv4ntez.Mk1Midi
ExecStart=$PY $DATA/mk1-midi/mk1_midi.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
cat > "$DATA/dbus-1/services/io.github.cerv4ntez.Mk1Midi.service" <<EOF
[D-BUS Service]
Name=io.github.cerv4ntez.Mk1Midi
Exec=$PY $DATA/mk1-midi/mk1_midi.py
SystemdService=mk1-midi.service
EOF
systemctl --user daemon-reload
systemctl --user enable mk1-midi.service >/dev/null
systemctl --user restart mk1-midi.service

echo "==> GNOME Shell extension"
DEST="$DATA/gnome-shell/extensions/$UUID"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -r "$HERE/extension/$UUID/." "$DEST/"
if ! gnome-extensions enable "$UUID" 2>/dev/null; then
    # A running Wayland shell only discovers new extensions after re-login;
    # pre-enable it so it starts then.
    "$PY" - "$UUID" <<'PYEOF'
import sys
from gi.repository import Gio
s = Gio.Settings.new('org.gnome.shell')
uuid = sys.argv[1]
on = s.get_strv('enabled-extensions')
if uuid not in on:
    s.set_strv('enabled-extensions', on + [uuid])
s.set_strv('disabled-extensions', [u for u in s.get_strv('disabled-extensions') if u != uuid])
Gio.Settings.sync()
PYEOF
fi

if (( INSTALL_UDEV )); then
    echo "==> udev rule (USB access for your login session, no mouse clicks from the Maschine; needs sudo)"
    sudo install -Dm644 "$HERE/system/70-maschine-mk1.rules" /etc/udev/rules.d/70-maschine-mk1.rules
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=input --action=change || true
    sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=17cc --action=change || true
fi

echo
echo "Done. MIDI port: 'Maschine MK1' (ALSA/PipeWire)."
if ! gnome-extensions info "$UUID" 2>/dev/null | grep -q 'State: ACTIVE'; then
    echo "Log out and back in once so GNOME Shell (Wayland) picks up the new extension;"
    echo "it is already enabled and will show up in Quick Settings."
fi
