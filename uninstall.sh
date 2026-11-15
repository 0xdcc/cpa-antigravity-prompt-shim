#!/usr/bin/env bash
set -euo pipefail
PREFIX=/opt/cpa-antigravity-prompt-shim
ETC=/etc/cpa-antigravity-prompt-shim
if [[ $EUID -ne 0 ]]; then echo 'run as root'; exit 1; fi
if [[ ${1:-} != --yes ]]; then echo 'This stops/removes only the shim. It does not change CPA or New API.'; read -r -p 'Type REMOVE to continue: ' x; [[ $x == REMOVE ]] || exit 2; fi
systemctl disable --now cpa-antigravity-prompt-shim.service 2>/dev/null || true
rm -f /etc/systemd/system/cpa-antigravity-prompt-shim.service
systemctl daemon-reload
rm -rf -- "$PREFIX"
if [[ ${KEEP_CONFIG:-0} != 1 ]]; then rm -rf -- "$ETC"; fi
printf '%s\n' 'Removed shim. Restore the relay/channel upstream separately if it was changed.'
