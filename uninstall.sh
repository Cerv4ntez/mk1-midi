#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Cerv4ntez
# Removes everything install.sh put in place. Keeps ~/.config/mk1-midi.
set -uo pipefail

UUID=mk1-midi@cerv4ntez.github.io
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"

gnome-extensions disable "$UUID" 2>/dev/null
rm -rf "$DATA/gnome-shell/extensions/$UUID"
systemctl --user disable --now mk1-midi.service 2>/dev/null
rm -f "$CONF/systemd/user/mk1-midi.service" "$DATA/dbus-1/services/io.github.cerv4ntez.Mk1Midi.service"
systemctl --user daemon-reload
rm -rf "$DATA/mk1-midi"
if [[ -f /etc/udev/rules.d/70-maschine-mk1.rules ]]; then
    sudo rm -f /etc/udev/rules.d/70-maschine-mk1.rules && sudo udevadm control --reload-rules
fi
echo "Removed. Your mapping is still in $CONF/mk1-midi/config.json"
