#!/usr/bin/env bash
set -euo pipefail
PREFIX=/opt/cpa-antigravity-prompt-shim
ETC=/etc/cpa-antigravity-prompt-shim
UNIT=/etc/systemd/system/cpa-antigravity-prompt-shim.service
YES=0
for arg in "$@"; do case "$arg" in --yes) YES=1;; --prefix) shift; PREFIX="$1";; --help|-h) printf '%s\n' 'Usage: sudo ./install.sh [--yes] [--prefix PATH]'; exit 0;; esac; done
if [[ $EUID -ne 0 ]]; then echo 'run as root'; exit 1; fi
if [[ $YES -ne 1 ]]; then
  echo "This installs an operator-side private relay at $PREFIX and $ETC."
  echo 'It does not modify CPA, New API, OAuth files, or user home configuration.'
  read -r -p 'Type INSTALL to continue: ' answer
  [[ $answer == INSTALL ]] || { echo 'cancelled'; exit 2; }
fi
install -d -m 0755 "$PREFIX" "$ETC"
install -m 0755 shim.py healthcheck.py "$PREFIX/"
install -m 0644 cpa-antigravity-prompt-shim.service "$ETC/../systemd/system/" 2>/dev/null || install -m 0644 cpa-antigravity-prompt-shim.service /etc/systemd/system/
if [[ ! -e "$ETC/config.toml" ]]; then install -m 0600 config.example.toml "$ETC/config.toml"; fi
if [[ ! -e "$ETC/mode" ]]; then printf 'wrap\n' > "$ETC/mode"; chmod 0600 "$ETC/mode"; fi
systemctl daemon-reload
systemctl enable --now cpa-antigravity-prompt-shim.service
"$PREFIX/healthcheck.py" --url http://127.0.0.1:8327/healthz
printf '%s\n' 'Installed. New API/channel routing is intentionally not changed by this script.'
