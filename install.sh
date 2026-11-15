#!/usr/bin/env bash
set -euo pipefail
PREFIX=/opt/cpa-antigravity-prompt-shim
SERVICE=cpa-antigravity-prompt-shim
CONFIG_TEMPLATE=config.example.toml
SUB2API_MODE=0
DESTDIR=
YES=0
DRY_RUN=0
while (($#)); do
  case "$1" in
    --yes) YES=1; shift;;
    --sub2api)
      SUB2API_MODE=1
      CONFIG_TEMPLATE=config.sub2api.example.toml
      [[ $SERVICE == cpa-antigravity-prompt-shim ]] && SERVICE=sub2api-antigravity-prompt-shim
      [[ $PREFIX == /opt/cpa-antigravity-prompt-shim ]] && PREFIX=/opt/sub2api-antigravity-prompt-shim
      shift;;
    --service-name) [[ $# -ge 2 ]] || { echo 'missing option value' >&2; exit 2; }; SERVICE=$2; shift 2;;
    --prefix|--destdir) [[ $# -ge 2 ]] || { echo 'missing option value' >&2; exit 2; }; key=$1; value=$2; shift 2; if [[ $key == --prefix ]]; then PREFIX=$value; else DESTDIR=$value; fi;;
    --dry-run) DRY_RUN=1; shift;;
    --help|-h) printf '%s\n' 'Usage: ./install.sh [--yes] [--sub2api] [--service-name <prefix>-antigravity-prompt-shim] [--prefix /absolute/path] [--destdir /staging/root] [--dry-run]'; exit 0;;
    *) echo 'unknown option' >&2; exit 2;;
  esac
done
# Restrict service names to dedicated *-antigravity-prompt-shim units so system services cannot be clobbered.
[[ $SERVICE =~ ^[a-z0-9][a-z0-9-]*-antigravity-prompt-shim$ ]] || { echo 'service-name must match <name>-antigravity-prompt-shim' >&2; exit 2; }
[[ $PREFIX =~ ^/[-a-zA-Z0-9_./]+$ && $PREFIX != / && $PREFIX != *..* ]] || { echo 'invalid prefix' >&2; exit 2; }
[[ -z $DESTDIR || ( $DESTDIR == /* && $DESTDIR != / && $DESTDIR != *..* ) ]] || { echo 'invalid destdir' >&2; exit 2; }
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if ((DRY_RUN)); then printf 'service=%s prefix=%s destdir=%s template=%s; no writes, no systemctl\n' "$SERVICE" "$PREFIX" "${DESTDIR:-/}" "$CONFIG_TEMPLATE"; exit 0; fi
if [[ -z $DESTDIR && $EUID -ne 0 ]]; then echo 'run as root for live installation' >&2; exit 1; fi
if ((YES != 1)); then
  read -r -p 'Type INSTALL to install this private loopback relay: ' answer
  [[ $answer == INSTALL ]] || exit 2
fi
ETC="$DESTDIR/etc/$SERVICE"
UNIT="$DESTDIR/etc/systemd/system/$SERVICE.service"
if [[ -L "$UNIT" ]]; then echo 'refusing to overwrite symlinked unit' >&2; exit 1; fi
if [[ -e "$UNIT" ]] && ! grep -q '^X-AntigravityPromptShimManaged=true$' "$UNIT"; then
  if ! grep -q 'antigravity-prompt-shim/shim\.py' "$UNIT"; then
    echo 'refusing to overwrite unmanaged systemd unit' >&2; exit 1
  fi
fi
install -d -m 0755 "$DESTDIR$PREFIX" "$ETC" "$(dirname -- "$UNIT")"
install -m 0755 "$SOURCE/shim.py" "$SOURCE/healthcheck.py" "$DESTDIR$PREFIX/"
install -m 0644 "$SOURCE/adapters.py" "$DESTDIR$PREFIX/"
# Generate the unit atomically without following symlinks or invoking shell interpolation.
python3 - "$SOURCE/cpa-antigravity-prompt-shim.service" "$UNIT" "$PREFIX" "$SERVICE" "$SUB2API_MODE" <<'PY'
import os, pathlib, sys, tempfile
source, target, prefix, service, sub2api_mode = sys.argv[1:]
text = pathlib.Path(source).read_text()
if sub2api_mode == "1":
    text = text.replace("Description=CPA Antigravity prompt shim (operator-side private relay)", "Description=Sub2API Antigravity prompt shim (operator-side private relay)")
    text = text.replace("After=network.target cli-proxy-api.service\nWants=cli-proxy-api.service", "After=network.target")
text = text.replace('/opt/cpa-antigravity-prompt-shim', prefix)
text = text.replace('/etc/cpa-antigravity-prompt-shim', f'/etc/{service}')
text = text.replace('RuntimeDirectory=cpa-antigravity-prompt-shim', f'RuntimeDirectory={service}')
text = text.replace('ReadWritePaths=/run/cpa-antigravity-prompt-shim', f'ReadWritePaths=/run/{service}')
target_path = pathlib.Path(target)
fd, tmp = tempfile.mkstemp(dir=str(target_path.parent), prefix=".unit-")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write(text)
os.chmod(tmp, 0o644)
os.replace(tmp, target_path)
PY
if [[ ! -e "$ETC/config.toml" ]]; then
  if [[ -L "$ETC/config.toml" ]]; then echo 'refusing symlinked config.toml' >&2; exit 1; fi
  python3 - "$SOURCE/$CONFIG_TEMPLATE" "$ETC/config.toml" "$SERVICE" <<'PY'
import os, pathlib, sys, tempfile
src, dst, service = sys.argv[1:]
text = pathlib.Path(src).read_text().replace('/etc/cpa-antigravity-prompt-shim', f'/etc/{service}')
dst_path = pathlib.Path(dst)
fd, tmp = tempfile.mkstemp(dir=str(dst_path.parent), prefix=".cfg-")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write(text)
os.chmod(tmp, 0o600)
os.replace(tmp, dst_path)
PY
fi
if [[ ! -e "$ETC/mode" ]]; then
  if [[ -L "$ETC/mode" ]]; then echo 'refusing symlinked mode' >&2; exit 1; fi
  printf 'wrap\n' > "$ETC/mode"
  chmod 0600 "$ETC/mode"
fi
if [[ -n $DESTDIR ]]; then echo 'Staged only; no service or healthcheck was run.'; exit 0; fi
systemctl daemon-reload
systemctl enable --now "$SERVICE.service"
HEALTH_URL=$(/usr/bin/python3 - "$ETC/config.toml" "$PREFIX" <<'PY'
import sys
sys.path.insert(0, sys.argv[2])
from shim import Config
config = Config.load(sys.argv[1])
print(f'http://{config.listen_host}:{config.listen_port}')
PY
)
/usr/bin/python3 "$PREFIX/healthcheck.py" --url "$HEALTH_URL"
printf '%s\n' 'Installed. Upstream service and channel routing were not changed.'
