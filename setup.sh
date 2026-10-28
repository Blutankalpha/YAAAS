#!/usr/bin/env bash
# Sets up YAAAS in a distrobox container (Ubuntu 24.04), so nothing gets installed on the host
# apart from distrobox and podman themselves. Run it on the host: bash setup.sh
set -euo pipefail
SELF=$(readlink -f "$0")
cd "$(dirname "$SELF")"

BOX=${YAAAS_BOX:-yaaas}
IMAGE=${YAAAS_IMAGE:-docker.io/library/ubuntu:24.04}
VENV=.venv-box
TORCH=auto
RECREATE=0
INSIDE=0

usage() {
    cat <<'EOF'
Usage: ./setup.sh [--cpu | --gpu] [--recreate]

  --cpu       install the CPU-only build of torch (much smaller download)
  --gpu       install the default torch build, which bundles CUDA (NVIDIA cards)
  --recreate  delete the container and build it again from scratch

With neither --cpu nor --gpu it picks --gpu when nvidia-smi is found on the host.
The container name and image can be changed with YAAAS_BOX and YAAAS_IMAGE.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --cpu) TORCH=cpu ;;
        --gpu) TORCH=gpu ;;
        --recreate) RECREATE=1 ;;
        --inside) INSIDE=1 ;;   # internal: the part that runs in the container
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $arg" >&2; usage >&2; exit 1 ;;
    esac
done

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

setup_inside() {
    command -v apt-get >/dev/null 2>&1 || die "this container isn't Debian/Ubuntu based; use the default image"
    local sudo=""
    [ "$(id -u)" -eq 0 ] || sudo=sudo

    say "Installing system packages in the container"
    $sudo apt-get update
    $sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3 python3-venv python3-pip python3-tk ffmpeg

    if [ ! -x "$VENV/bin/python" ]; then
        say "Creating $VENV"
        python3 -m venv "$VENV"
    fi
    local pip="$VENV/bin/pip"
    "$pip" install --upgrade pip wheel

    if [ "$TORCH" = auto ]; then
        if command -v nvidia-smi >/dev/null 2>&1; then TORCH=gpu; else TORCH=cpu; fi
    fi
    if [ "$TORCH" = cpu ]; then
        say "Installing CPU-only torch"
        "$pip" install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
    else
        say "Using the default torch build (CUDA); it's a large download"
    fi

    say "Installing Python packages"
    "$pip" install yt-dlp ytmusicapi rapidfuzz requests mutagen numpy demucs whisperx faster-whisper

    say "Checking the install"
    "$VENV/bin/python" - <<'EOF' || warn "some packages failed to import, see above"
import importlib
bad = []
for m in ("yt_dlp", "ytmusicapi", "rapidfuzz", "requests", "mutagen", "numpy", "tkinter",
          "torch", "demucs.separate", "whisperx", "faster_whisper"):
    try:
        importlib.import_module(m)
    except Exception as e:
        bad.append(f"  {m}: {e}")
if bad:
    print("\n".join(bad))
    raise SystemExit(1)
print("all packages import fine")
EOF
}

if [ "$INSIDE" = 1 ]; then
    setup_inside
    exit 0
fi

# ---- host side

[ -f yaaas.py ] || die "yaaas.py isn't next to this script"
[ -z "${CONTAINER_ID:-}" ] || die "run setup.sh on the host, not inside a container"

# distrobox shares your home directory with the container, nothing else
home_real=$(readlink -f "$HOME")
case "$PWD" in
    "$home_real"/*|"$HOME"/*) ;;
    *) die "keep this folder inside your home directory, the container can't see it otherwise" ;;
esac

if command -v dnf >/dev/null 2>&1; then PM=dnf
elif command -v apt-get >/dev/null 2>&1; then PM=apt
elif command -v pacman >/dev/null 2>&1; then PM=pacman
else PM=""
fi

need=()
command -v distrobox >/dev/null 2>&1 || need+=(distrobox)
if ! command -v podman >/dev/null 2>&1 && ! command -v docker >/dev/null 2>&1; then
    need+=(podman)
fi
if [ "${#need[@]}" -gt 0 ]; then
    if [ -e /run/ostree-booted ]; then
        die "install ${need[*]} with rpm-ostree (or your image's package manager), reboot, then run this again"
    fi
    say "Installing ${need[*]} on the host"
    sudo=""
    if [ "$(id -u)" -ne 0 ]; then
        command -v sudo >/dev/null 2>&1 || die "need root or sudo to install ${need[*]}"
        sudo=sudo
    fi
    case "$PM" in
        dnf)    $sudo dnf install -y "${need[@]}" ;;
        apt)    $sudo apt-get update && $sudo apt-get install -y "${need[@]}" ;;
        pacman) $sudo pacman -S --needed --noconfirm "${need[@]}" ;;
        *)      die "don't know how to install ${need[*]} here; see https://distrobox.it" ;;
    esac
fi

if [ "$TORCH" = auto ]; then
    if command -v nvidia-smi >/dev/null 2>&1; then TORCH=gpu; else TORCH=cpu; fi
fi

box_exists() {
    local names
    names=$(distrobox list --no-color 2>/dev/null | awk -F'|' 'NR > 1 { gsub(/[ \t]/, "", $2); print $2 }' || true)
    grep -qx "$BOX" <<<"$names"
}

if box_exists && [ "$RECREATE" = 1 ]; then
    say "Removing the existing '$BOX' container"
    distrobox rm --force "$BOX"
fi

if box_exists; then
    say "Container '$BOX' already exists, reusing it"
    if [ "$TORCH" = gpu ]; then
        warn "if the GPU isn't visible in it, run again with --recreate (the container is made with --nvidia)"
    fi
else
    say "Creating the '$BOX' container from $IMAGE"
    create_args=(create --name "$BOX" --image "$IMAGE" --yes)
    [ "$TORCH" = gpu ] && create_args+=(--nvidia)
    distrobox "${create_args[@]}"
fi

say "Setting up inside the container (the first start also sets the container up, which takes a while)"
distrobox enter "$BOX" -- bash "$SELF" --inside "--$TORCH"

chmod +x run.sh yaaas.py yaaas_gui.py 2>/dev/null || true
say "Done. Start it with ./run.sh"
