#!/usr/bin/env bash
set -euo pipefail
PREFIX=/opt/cpa-antigravity-prompt-shim
DESTDIR=
YES=0
DRY_RUN=0
while (($#)); do
  case "$1" in
    --yes) YES=1; shift;;
    --prefix|--destdir) [[ $# -ge 2 ]] || { echo 'missing option value' >&2; exit 2; }; key=$1; value=$2; shift 2; if [[ $key == --prefix ]]; then PREFIX=$value; else DESTDIR=$value; fi;;
    --dry-run) DRY_RUN=1; shift;;
    --help|-h) printf '%s\n' 'Usage: ./install.sh [--yes] [--prefix /absolute/path] [--destdir /staging/root] [--dry-run]'; exit 0;;
    *) echo 'unknown option' >&2; exit 2;;
  esac
done
# Restrict unit-substitution input to simple absolute paths.
[[ $PREFIX =~ ^/[-a-zA-Z0-9_./]+$ && $PREFIX != / && $PREFIX != *..* ]] || { echo 'invalid prefix' >&2; exit 2; }
[[ -z $DESTDIR || ( $DESTDIR == /* && $DESTDIR != / && $DESTDIR != *..* ) ]] || { echo 'invalid destdir' >&2; exit 2; }
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if ((DRY_RUN)); then printf 'prefix=%s destdir=%s; no writes, no systemctl\n' "$PREFIX" "${DESTDIR:-/}"; exit 0; fi
if [[ -z $DESTDIR && $EUID -ne 0 ]]; then echo 'run as root for live installation' >&2; exit 1; fi
if ((YES != 1)); then
  read -r -p 'Type INSTALL to install this private loopback relay: ' answer
  [[ $answer == INSTALL ]] || exit 2
fi
ETC="$DESTDIR/etc/cpa-antigravity-prompt-shim"
UNIT="$DESTDIR/etc/systemd/system/cpa-antigravity-prompt-shim.service"
install -d -m 0755 "$DESTDIR$PREFIX" "$ETC" "$(dirname -- "$UNIT")"
install -m 0755 "$SOURCE/shim.py" "$SOURCE/healthcheck.py" "$DESTDIR$PREFIX/"
install -m 0644 "$SOURCE/adapters.py" "$DESTDIR$PREFIX/"
# Generate the unit without invoking shell interpolation in the service command.
python3 - "$SOURCE/cpa-antigravity-prompt-shim.service" "$UNIT" "$PREFIX" <<'PY'
import pathlib, sys
source, target, prefix = sys.argv[1:]
pathlib.Path(target).write_text(pathlib.Path(source).read_text().replace('/opt/cpa-antigravity-prompt-shim', prefix))
PY
chmod 0644 "$UNIT"
if [[ ! -e "$ETC/config.toml" ]]; then install -m 0600 "$SOURCE/config.example.toml" "$ETC/config.toml"; fi
if [[ ! -e "$ETC/mode" ]]; then printf 'wrap\n' > "$ETC/mode"; chmod 0600 "$ETC/mode"; fi
if [[ -n $DESTDIR ]]; then echo 'Staged only; no service or healthcheck was run.'; exit 0; fi
systemctl daemon-reload
systemctl enable --now cpa-antigravity-prompt-shim.service
HEALTH_URL=$(/usr/bin/python3 - "$ETC/config.toml" "$PREFIX" <<'PY'
import sys
sys.path.insert(0, sys.argv[2])
from shim import Config
config = Config.load(sys.argv[1])
print(f'http://{config.listen_host}:{config.listen_port}')
PY
)
/usr/bin/python3 "$PREFIX/healthcheck.py" --url "$HEALTH_URL"
printf '%s\n' 'Installed. CPA and channel routing were not changed.'
