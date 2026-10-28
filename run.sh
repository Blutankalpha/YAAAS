#!/usr/bin/env bash
# Starts YAAAS inside its distrobox container.
#   ./run.sh                          opens the GUI
#   ./run.sh --nogui songs.txt -o ~/Music
#   ./run.sh --nogui --scan ~/Music   --nogui switches to the command line version
#   ./run.sh --nogui --help           lists the command line options
set -euo pipefail
SELF=$(readlink -f "$0")
cd "$(dirname "$SELF")"

BOX=${YAAAS_BOX:-yaaas}
PY=.venv-box/bin/python

if [ "${CONTAINER_ID:-}" != "$BOX" ]; then
    # on the host: hop into the container and run this same script there
    command -v distrobox >/dev/null 2>&1 || { echo "distrobox isn't installed. Run ./setup.sh first." >&2; exit 1; }
    names=$(distrobox list --no-color 2>/dev/null | awk -F'|' 'NR > 1 { gsub(/[ \t]/, "", $2); print $2 }' || true)
    if ! grep -qx "$BOX" <<<"$names"; then
        echo "There's no '$BOX' container yet. Run ./setup.sh first." >&2
        exit 1
    fi
    exec distrobox enter "$BOX" -- bash "$SELF" "$@"
fi

# ---- inside the container

if [ ! -x "$PY" ]; then
    echo "The environment in the container is missing. Run ./setup.sh first." >&2
    exit 1
fi

# links and files should open in the host's browser / player
if command -v distrobox-host-exec >/dev/null 2>&1; then
    export BROWSER="distrobox-host-exec xdg-open %s"
fi

# faster-whisper (ctranslate2) needs the CUDA libraries pip put under site-packages/nvidia
NVLIBS=$("$PY" - <<'EOF' 2>/dev/null || true
import glob, os, site
dirs = []
for sp in site.getsitepackages():
    dirs += glob.glob(os.path.join(sp, "nvidia", "*", "lib"))
print(os.pathsep.join(dirs))
EOF
)
if [ -n "$NVLIBS" ]; then
    export LD_LIBRARY_PATH="$NVLIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# the GUI is the default; --nogui (or --help) hands everything else to the command line version
gui=1
args=()
for arg in "$@"; do
    case "$arg" in
        --nogui) gui=0 ;;
        --gui) ;;
        -h|--help) gui=0; args+=("$arg") ;;
        *) args+=("$arg") ;;
    esac
done

if [ "$gui" = 1 ]; then
    if [ "${#args[@]}" -gt 0 ]; then
        echo "note: the GUI ignores arguments; add --nogui to use the command line version" >&2
    fi
    exec "$PY" yaaas_gui.py
fi
exec "$PY" yaaas.py ${args[@]+"${args[@]}"}
