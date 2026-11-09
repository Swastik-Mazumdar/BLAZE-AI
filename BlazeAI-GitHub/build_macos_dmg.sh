#!/bin/bash
# Build Blaze AI.app (macOS) and package it as a DMG with the app logo.
#
#   ./build_macos_dmg.sh --voice --models   # full build (recommended)
#   ./build_macos_dmg.sh                    # core UI + butler only
#   ./build_macos_dmg.sh --dmg-only         # package existing dist/Blaze AI.app
#
# Output (kept separate from pkg builds):
#   dist/dmg/BlazeAI-<version>.dmg
#
# The app is staged outside the project before signing/packaging: an
# iCloud-synced Desktop can re-add Finder metadata that breaks signatures.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This DMG builder must run on macOS." >&2
  exit 2
fi

INCLUDE_VOICE=0
INCLUDE_MODELS=0
DMG_ONLY=0
for ARG in "$@"; do
  case "$ARG" in
    --voice) INCLUDE_VOICE=1 ;;
    --models) INCLUDE_MODELS=1 ;;
    --dmg-only) DMG_ONLY=1 ;;
    *) echo "Unknown option: $ARG" >&2; exit 2 ;;
  esac
done

PYTHON="${BLAZE_PYTHON:-python3}"
if [ "$DMG_ONLY" != 1 ]; then
  if [ ! -d .venv ]; then
    "$PYTHON" -m venv .venv
  fi
  VENV_PY="$ROOT/.venv/bin/python"
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PY" -m pip install -r requirements.txt -r requirements-build.txt
  if [ "$INCLUDE_VOICE" = 1 ]; then
    "$VENV_PY" -m pip install -r requirements-voice.txt
  fi
  if [ "$INCLUDE_MODELS" = 1 ]; then
    "$VENV_PY" -m pip install -r requirements-local-model.txt
    BLAZE_BUNDLE_LOCAL_MODELS=1 "$VENV_PY" -m PyInstaller --clean --noconfirm BlazeAI.spec
  else
    "$VENV_PY" -m PyInstaller --clean --noconfirm BlazeAI.spec
  fi
else
  VENV_PY="$ROOT/.venv/bin/python"
  if [ ! -x "$VENV_PY" ]; then
    VENV_PY="$PYTHON"
  fi
fi

APP_PATH="$ROOT/dist/Blaze AI.app"
VERSION="$("$VENV_PY" -c 'from blaze_ai.version import VERSION; print(VERSION)' 2>/dev/null || echo 1.1.0)"
DMG_DIR="$ROOT/dist/dmg"
DMG_PATH="$DMG_DIR/BlazeAI-${VERSION}.dmg"
VOL_NAME="Blaze AI"
ICON_SRC="$ROOT/blaze_ai/assets/blaze_icon.icns"

if [ ! -d "$APP_PATH" ]; then
  echo "Build finished but $APP_PATH was not found." >&2
  echo "Run without --dmg-only to build the app first." >&2
  exit 1
fi

# Ship the Prism (Ternary-Bonsai-2) runtime untouched. PyInstaller rewrites
# Mach-O install names/rpaths it collects; copying the pristine folder keeps
# @executable_path relative loading intact.
PRISM_SRC="$ROOT/runtimes/llama-bonsai"
PRISM_DST="$APP_PATH/Contents/Frameworks/llama-bonsai"
if [ -d "$PRISM_SRC" ]; then
  mkdir -p "$(dirname "$PRISM_DST")"
  rm -rf "$PRISM_DST"
  ditto "$PRISM_SRC" "$PRISM_DST" 2>/dev/null || cp -R "$PRISM_SRC" "$(dirname "$PRISM_DST")/"
  echo "Bundled Prism runtime from $PRISM_SRC"
fi

# HTTPS model search/download needs the certifi CA bundle inside the app.
if ! find "$APP_PATH/Contents" -name "cacert.pem" -print -quit | grep -q .; then
  echo "WARNING: certifi cacert.pem was not bundled; model download may fail with" >&2
  echo "CERTIFICATE_VERIFY_FAILED. Reinstall requirements.txt and rebuild." >&2
fi

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/blaze-dmg.XXXXXX")"
MOUNT_DIR=""
cleanup() {
  if [ -n "$MOUNT_DIR" ] && mount | grep -q "on $MOUNT_DIR "; then
    hdiutil detach "$MOUNT_DIR" -quiet || hdiutil detach "$MOUNT_DIR" -force -quiet || true
  fi
  rm -rf "$STAGE"
}
trap cleanup EXIT

mkdir -p "$DMG_DIR"
rm -f "$DMG_PATH"
rm -f "$DMG_DIR"/rw-*.dmg 2>/dev/null || true

# Copy without resource forks/extended attributes, then ad-hoc sign locally.
ditto --norsrc --noextattr "$APP_PATH" "$STAGE/Blaze AI.app"
xattr -cr "$STAGE/Blaze AI.app" 2>/dev/null || true
codesign --force --deep --sign - "$STAGE/Blaze AI.app"
codesign --verify --deep --strict "$STAGE/Blaze AI.app"

# Prepare DMG contents: app + Applications drag target + volume icon (logo).
DMG_SRC="$STAGE/dmg-src"
mkdir -p "$DMG_SRC"
ditto "$STAGE/Blaze AI.app" "$DMG_SRC/Blaze AI.app"
ln -s /Applications "$DMG_SRC/Applications"
if [ -f "$ROOT/packaging/macos/README.md" ]; then
  cp "$ROOT/packaging/macos/README.md" "$DMG_SRC/README - Blaze AI.md"
fi
if [ -f "$ICON_SRC" ]; then
  cp "$ICON_SRC" "$DMG_SRC/.VolumeIcon.icns"
fi

RW_DMG="$DMG_DIR/rw-BlazeAI-${VERSION}.dmg"
hdiutil create -volname "$VOL_NAME" -srcfolder "$DMG_SRC" -ov -format UDRW "$RW_DMG" >/dev/null

# Attach and mark the custom volume icon (Blaze logo).
MOUNT_DIR="$STAGE/mnt"
mkdir -p "$MOUNT_DIR"
ATTACH_OUT="$(hdiutil attach "$RW_DMG" -mountpoint "$MOUNT_DIR" -nobrowse -noverify -noautoopen)"
if [ -f "$MOUNT_DIR/.VolumeIcon.icns" ]; then
  # SetFile may live in Xcode CLT; harmless if unavailable.
  if command -v SetFile >/dev/null 2>&1; then
    SetFile -c icnC "$MOUNT_DIR/.VolumeIcon.icns" 2>/dev/null || true
    SetFile -a C "$MOUNT_DIR" 2>/dev/null || true
  elif xcrun -f SetFile >/dev/null 2>&1; then
    xcrun SetFile -c icnC "$MOUNT_DIR/.VolumeIcon.icns" 2>/dev/null || true
    xcrun SetFile -a C "$MOUNT_DIR" 2>/dev/null || true
  fi
  # Also set Finder volume icon flag via AppleScript-free attribute where possible.
  # (SetFile -a C is the important one; modern macOS still honors it.)
fi
sync
hdiutil detach "$MOUNT_DIR" -quiet || hdiutil detach "$MOUNT_DIR" -force -quiet
MOUNT_DIR=""

hdiutil convert "$RW_DMG" -format UDZO -imagekey zlib-level=9 -o "$DMG_PATH" >/dev/null
rm -f "$RW_DMG"

echo "Built $DMG_PATH · Blaze AI $VERSION"
echo "DMG includes Blaze AI.app (with app icon), Applications shortcut, and volume logo."
echo "Every bundled runtime/folder from the PyInstaller app (assets, certifi CA, optional runtimes/deps) is inside Blaze AI.app."
