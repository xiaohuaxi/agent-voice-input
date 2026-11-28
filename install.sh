#!/bin/sh
# Minimal, auditable installer for agent-voice-input (macOS & Linux/WSL2).
# Uses uv for an isolated Python >= 3.10 install; never runs sudo or edits shell rc.
set -eu

PKG="agent-voice-input"
SPEC="${AVI_FROM:-$PKG}"
DRY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --from)    [ $# -ge 2 ] || { echo "error: --from needs a value" >&2; exit 2; }; SPEC="$2"; shift 2 ;;
    --version) [ $# -ge 2 ] || { echo "error: --version needs a value" >&2; exit 2; }; SPEC="${PKG}==$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) printf 'Usage: install.sh [--version VER] [--from PATH_OR_SPEC] [--dry-run]\n'; exit 0 ;;
    *) printf 'error: unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done
[ -n "${AVI_VERSION:-}" ] && [ "$SPEC" = "$PKG" ] && SPEC="${PKG}==${AVI_VERSION}"

OS="$(uname -s 2>/dev/null || printf unknown)"
IS_WSL=0
[ "$OS" = "Linux" ] && [ -r /proc/version ] && grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null && IS_WSL=1
if [ "$OS" = "Linux" ] && [ "$IS_WSL" -eq 0 ]; then
  printf 'Note: Native Linux desktop daemon/hotkeys are not supported yet (CLI works for once --file / SSH).\n'
elif [ "$OS" != "Darwin" ] && [ "$OS" != "Linux" ]; then
  printf 'error: unsupported OS (%s). Supports macOS and Windows + WSL2.\n' "$OS" >&2; exit 1
fi

TMP_UV=""
cleanup() {
  if [ -n "${TMP_UV:-}" ]; then
    rm -f "$TMP_UV"
    TMP_UV=""
  fi
}
trap cleanup EXIT
trap 'cleanup; exit 129' HUP
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

find_uv() {
  _u="$(command -v uv 2>/dev/null || true)"
  if [ -n "$_u" ] && [ -x "$_u" ]; then
    printf '%s\n' "$_u"
    return 0
  fi
  _uv_dir_bin=""
  [ -n "${UV_INSTALL_DIR:-}" ] && _uv_dir_bin="${UV_INSTALL_DIR%/}/uv"
  _xdg_bin=""
  [ -n "${XDG_BIN_HOME:-}" ] && _xdg_bin="${XDG_BIN_HOME%/}/uv"
  _xdg_data_bin=""
  if [ -n "${XDG_DATA_HOME:-}" ]; then
    _xdg_data_bin="${XDG_DATA_HOME%/}/../bin/uv"
    [ -x "$_xdg_data_bin" ] || _xdg_data_bin="$(dirname "$XDG_DATA_HOME")/bin/uv"
  fi
  for _c in \
    "$_uv_dir_bin" \
    "$_xdg_bin" \
    "$_xdg_data_bin" \
    "$HOME/.local/bin/uv" \
    "$HOME/.cargo/bin/uv" \
    /opt/homebrew/bin/uv \
    /usr/local/bin/uv; do
    if [ -n "$_c" ] && [ -x "$_c" ]; then
      printf '%s\n' "$_c"
      return 0
    fi
  done
  return 1
}

UV_BIN="$(find_uv || true)"

if [ -z "$UV_BIN" ]; then
  printf 'uv not found; installing uv via official user-level installer (no sudo, no shell rc edits)...\n'
  if [ "$DRY" -eq 1 ]; then
    printf "[dry-run] tmp=\$(mktemp) && curl -fsSL https://astral.sh/uv/install.sh -o \"\$tmp\" && env UV_NO_MODIFY_PATH=1 sh \"\$tmp\" && rm -f \"\$tmp\"\n"
    UV_BIN="uv"
  else
    TMP_UV="$(mktemp)"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL https://astral.sh/uv/install.sh -o "$TMP_UV"
    elif command -v wget >/dev/null 2>&1; then
      wget -qO "$TMP_UV" https://astral.sh/uv/install.sh
    else
      printf 'error: curl or wget is required to install uv\n' >&2; exit 1
    fi
    env UV_NO_MODIFY_PATH=1 sh "$TMP_UV"
    rm -f "$TMP_UV"
    TMP_UV=""
    UV_BIN="$(find_uv || true)"
    if [ -z "$UV_BIN" ] || [ ! -x "$UV_BIN" ]; then
      printf 'error: uv binary not found after install\n' >&2
      exit 1
    fi
  fi
fi

printf 'Installing %s via %s (Python >= 3.10)...\n' "$SPEC" "$UV_BIN"
if [ "$DRY" -eq 1 ]; then
  printf "[dry-run] %s tool install --upgrade --python '>=3.10' %s\n" "$UV_BIN" "$SPEC"
else
  "$UV_BIN" tool install --upgrade --python '>=3.10' "$SPEC"
  BIN_DIR="$("$UV_BIN" tool dir --bin 2>/dev/null || printf '%s/.local/bin' "$HOME")"
  AVI_BIN="$BIN_DIR/$PKG"
  [ -x "$AVI_BIN" ] || { printf 'error: %s not found after install\n' "$AVI_BIN" >&2; exit 1; }
  version="$("$AVI_BIN" --version)"
  printf 'Installed: %s (%s)\n' "$AVI_BIN" "$version"
  PATH_BIN="$(command -v "$PKG" 2>/dev/null || true)"
  if [ -z "$PATH_BIN" ]; then
    printf '\nPATH note: add %s to your PATH:\n  export PATH="%s:%s"\n' "$BIN_DIR" "$BIN_DIR" "\$PATH"
  elif [ "$PATH_BIN" != "$AVI_BIN" ]; then
    printf '\nPATH note: current PATH resolves %s before %s\n' "$PATH_BIN" "$AVI_BIN"
  fi
fi

MISSING=0
if ! command -v ffmpeg >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/ffmpeg ] && [ ! -x /usr/local/bin/ffmpeg ]; then
  MISSING=1
  if [ "$OS" = "Darwin" ]; then
    printf '\nMissing ffmpeg. Install with:\n  brew install ffmpeg\n'
  elif command -v apt-get >/dev/null 2>&1; then
    printf '\nMissing ffmpeg. Install with:\n  sudo apt update && sudo apt install -y ffmpeg\n'
  elif command -v dnf >/dev/null 2>&1; then
    printf '\nMissing ffmpeg. Install with:\n  sudo dnf install -y ffmpeg\n'
  elif command -v pacman >/dev/null 2>&1; then
    printf '\nMissing ffmpeg. Install with:\n  sudo pacman -S ffmpeg\n'
  else
    printf '\nMissing ffmpeg. Please install ffmpeg via your system package manager.\n'
  fi
fi
if [ "$OS" = "Darwin" ]; then
  if ! xcode-select -p >/dev/null 2>&1 || ! xcrun --find swiftc >/dev/null 2>&1; then
    MISSING=1
    printf '\nMissing Xcode Command Line Tools (swiftc). Install with:\n  xcode-select --install\n'
  fi
fi

printf '\nNext steps:\n'
printf '  1. %s init\n' "$PKG"
printf '  2. Edit ~/.agent-voice-input/.env: uncomment (remove leading #) and add 1 ASR API key\n'
if [ "$OS" = "Darwin" ]; then
  printf '  3. In a local GUI terminal (not SSH): %s doctor --ping && %s start\n' "$PKG" "$PKG"
  [ "$MISSING" -eq 0 ] || printf '  (Install the missing host dependency above before running doctor/start.)\n'
elif [ "$IS_WSL" -eq 1 ]; then
  printf '  3. WSL detected — confirm this distro uses WSL2 (not WSL1), then run: %s doctor --ping && %s start\n' "$PKG" "$PKG"
  [ "$MISSING" -eq 0 ] || printf '  (Install the missing host dependency above before running doctor/start.)\n'
else
  printf '  3. %s doctor --ping\n' "$PKG"
  printf '  (Note: the background daemon / hotkey start is currently supported on macOS and Windows + WSL2 only; on native Linux use: %s once --file <wav> --dry-run)\n' "$PKG"
  [ "$MISSING" -eq 0 ] || printf '  (Install the missing host dependency above before running doctor.)\n'
fi
