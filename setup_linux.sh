#!/usr/bin/env bash
# Sets up YAAAS directly on this machine: system packages, a Python venv in ./.venv,
# and the Python dependencies. (setup.sh does the same inside a distrobox container.)
# Run it from anywhere: bash setup_linux.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

VENV=.venv
TORCH=auto

usage() {
    cat <<'EOF'
Usage: ./setup_linux.sh [--cpu | --gpu]

  --cpu   install the CPU-only build of torch (much smaller download)
  --gpu   install the default torch build, which bundles CUDA (NVIDIA cards)

With neither flag it picks --gpu when nvidia-smi is found, otherwise --cpu.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --cpu) TORCH=cpu ;;
        --gpu) TORCH=gpu ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $arg" >&2; usage >&2; exit 1 ;;
    esac
done

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

[ -f yaaas.py ] || die "yaaas.py isn't next to this script"

if command -v dnf >/dev/null 2>&1; then PM=dnf
elif command -v apt-get >/dev/null 2>&1; then PM=apt
elif command -v pacman >/dev/null 2>&1; then PM=pacman
else PM=""
fi

pm_install() {
    local sudo=""
    if [ "$(id -u)" -ne 0 ]; then
        command -v sudo >/dev/null 2>&1 || die "need root or sudo to install: $*"
        sudo=sudo
    fi
    case "$PM" in
        dnf)    $sudo dnf install -y "$@" ;;
        apt)    $sudo apt-get update && $sudo apt-get install -y "$@" ;;
        pacman) $sudo pacman -S --needed --noconfirm "$@" ;;
        *)      die "don't know how to install '$*' on this system; install it yourself and run setup again" ;;
    esac
}

# whisperx and torch lag behind new Python releases, so prefer 3.12 or older
find_python() {
    local v
    for v in 3.12 3.11 3.10; do
        if command -v "python$v" >/dev/null 2>&1; then
            echo "python$v"
            return 0
        fi
    done
    return 1
}

if ! PY=$(find_python); then
    say "No Python 3.10-3.12 found, installing one"
    case "$PM" in
        dnf)    pm_install python3.12 ;;
        apt)    pm_install python3 python3-venv ;;
        pacman) pm_install python ;;
        *)      pm_install python3 ;;
    esac
    PY=$(find_python) || PY=python3
fi
command -v "$PY" >/dev/null 2>&1 || die "no usable python found"
if ! "$PY" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)'; then
    warn "$PY is $("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])'); torch/whisperx may not have wheels for it yet"
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    say "Installing ffmpeg"
    case "$PM" in
        dnf) pm_install ffmpeg-free ;;
        *)   pm_install ffmpeg ;;
    esac
fi

if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
    say "Installing Tk for the GUI"
    PYVER=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    case "$PM" in
        dnf)    pm_install "python${PYVER}-tkinter" || pm_install python3-tkinter ;;
        apt)    pm_install python3-tk ;;
        pacman) pm_install tk ;;
    esac
    "$PY" -c "import tkinter" >/dev/null 2>&1 \
        || warn "tkinter still isn't available, so the GUI won't start (use --nogui for the command line)"
fi

if [ ! -x "$VENV/bin/python" ]; then
    say "Creating $VENV with $PY"
    "$PY" -m venv "$VENV" || die "couldn't create a venv (on Debian/Ubuntu: sudo apt install python3-venv)"
fi
PIP="$VENV/bin/pip"
"$PIP" install --upgrade pip wheel

if [ "$TORCH" = auto ]; then
    if command -v nvidia-smi >/dev/null 2>&1; then TORCH=gpu; else TORCH=cpu; fi
fi
if [ "$TORCH" = cpu ]; then
    say "Installing CPU-only torch"
    "$PIP" install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
else
    say "Using the default torch build (CUDA); it's a large download"
fi

say "Installing Python packages"
"$PIP" install yt-dlp ytmusicapi rapidfuzz requests mutagen numpy demucs whisperx faster-whisper

say "Checking the install"
check=$("$VENV/bin/python" yaaas.py --check 2>&1) && ok=1 || ok=0
echo "$check"
[ "$ok" = 1 ] || warn "the check failed, see above"
if [ "$TORCH" = gpu ] && grep -q "cuda available: False" <<<"$check"; then
    warn "no CUDA GPU is visible to torch. Check the NVIDIA driver, or run again with --cpu"
fi

if [ "$PM" = dnf ] && ! ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libmp3lame; then
    warn "Fedora's ffmpeg-free can't encode mp3. Pick opus, m4a or flac in YAAAS, or enable RPM Fusion and run:"
    warn "  sudo dnf swap ffmpeg-free ffmpeg --allowerasing"
fi

chmod +x run_linux.sh yaaas.py yaaas_gui.py 2>/dev/null || true
say "Done. Start it with ./run_linux.sh"
