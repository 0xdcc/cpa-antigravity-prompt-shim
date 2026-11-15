#!/usr/bin/env bash
set -euo pipefail
PREFIX=/opt/cpa-antigravity-prompt-shim
ETC=/etc/cpa-antigravity-prompt-shim
YES=0
while (($#)); do
  case "$1" in
    --yes) YES=1; shift;;
    --prefix) [[ $# -ge 2 ]] || exit 2; PREFIX=$2; shift 2;;
    --help|-h) printf '%s\n' 'Usage: sudo ./uninstall.sh [--yes] [--prefix /absolute/path]'; exit 0;;
    *) echo 'unknown option' >&2; exit 2;;
  esac
done
[[ $PREFIX =~ ^/[-a-zA-Z0-9_./]+$ && $PREFIX != / && $PREFIX != *..* ]] || exit 2
if [[ $EUID -ne 0 ]]; then echo 'run as root'; exit 1; fi
if ((YES != 1)); then echo 'This stops/removes only the shim. It does not change CPA or New API.'; read -r -p 'Type REMOVE to continue: ' x; [[ $x == REMOVE ]] || exit 2; fi
systemctl disable --now cpa-antigravity-prompt-shim.service
rm -f /etc/systemd/system/cpa-antigravity-prompt-shim.service
systemctl daemon-reload
# Do not recursively remove arbitrary custom prefixes or unrelated config.
rm -f -- "$PREFIX/shim.py" "$PREFIX/adapters.py" "$PREFIX/healthcheck.py"
rmdir -- "$PREFIX" 2>/dev/null || true
if [[ ${KEEP_CONFIG:-0} != 1 ]]; then rm -f -- "$ETC/config.toml" "$ETC/mode"; rmdir -- "$ETC" 2>/dev/null || true; fi
printf '%s\n' 'Removed shim. Restore the relay/channel upstream separately if it was changed.'
