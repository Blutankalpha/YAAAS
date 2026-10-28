#!/usr/bin/env bash
# Starts YAAAS from the venv made by setup_linux.sh (no container).
#   ./run_linux.sh                          opens the GUI
#   ./run_linux.sh --nogui songs.txt -o ~/Music
#   ./run_linux.sh --nogui --scan ~/Music   --nogui switches to the command line version
#   ./run_linux.sh --nogui --help           lists the command line options
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
    echo "No environment yet. Run ./setup_linux.sh first." >&2
    exit 1
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
