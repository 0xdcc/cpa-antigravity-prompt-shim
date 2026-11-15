#!/usr/bin/env bash
set -euo pipefail
PREFIX=/opt/cpa-antigravity-prompt-shim
SERVICE=cpa-antigravity-prompt-shim
YES=0
while (($#)); do
  case "$1" in
    --yes) YES=1; shift;;
    --sub2api)
      [[ $SERVICE == cpa-antigravity-prompt-shim ]] && SERVICE=sub2api-antigravity-prompt-shim
      [[ $PREFIX == /opt/cpa-antigravity-prompt-shim ]] && PREFIX=/opt/sub2api-antigravity-prompt-shim
      shift;;
    --service-name) [[ $# -ge 2 ]] || exit 2; SERVICE=$2; shift 2;;
    --prefix) [[ $# -ge 2 ]] || exit 2; PREFIX=$2; shift 2;;
    --help|-h) printf '%s\n' 'Usage: sudo ./uninstall.sh [--yes] [--sub2api] [--service-name <prefix>-antigravity-prompt-shim] [--prefix /absolute/path]'; exit 0;;
    *) echo 'unknown option' >&2; exit 2;;
  esac
done
[[ $SERVICE =~ ^[a-z0-9][a-z0-9-]*-antigravity-prompt-shim$ ]] || exit 2
[[ $PREFIX =~ ^/[-a-zA-Z0-9_./]+$ && $PREFIX != / && $PREFIX != *..* ]] || exit 2
ETC="/etc/$SERVICE"
UNIT="/etc/systemd/system/$SERVICE.service"
if [[ $EUID -ne 0 ]]; then echo 'run as root'; exit 1; fi
if [[ -L "$UNIT" ]]; then
  echo 'refusing to uninstall symlinked systemd unit' >&2
  exit 1
fi
if [[ -e "$UNIT" ]] && ! grep -q '^X-AntigravityPromptShimManaged=true$' "$UNIT" && ! grep -q 'antigravity-prompt-shim/shim\.py' "$UNIT"; then
  echo 'refusing to uninstall unmanaged systemd unit' >&2
  exit 1
fi
if ((YES != 1)); then echo 'This stops/removes only the shim. It does not change CPA, Sub2API, or New API.'; read -r -p 'Type REMOVE to continue: ' x; [[ $x == REMOVE ]] || exit 2; fi
systemctl disable --now "$SERVICE.service"
rm -f -- "$UNIT"
systemctl daemon-reload
# Do not recursively remove arbitrary custom prefixes or unrelated config.
rm -f -- "$PREFIX/shim.py" "$PREFIX/adapters.py" "$PREFIX/healthcheck.py"
rmdir -- "$PREFIX" 2>/dev/null || true
if [[ ${KEEP_CONFIG:-0} != 1 ]]; then rm -f -- "$ETC/config.toml" "$ETC/mode"; rmdir -- "$ETC" 2>/dev/null || true; fi
printf '%s\n' 'Removed shim. Restore the relay/channel upstream separately if it was changed.'
