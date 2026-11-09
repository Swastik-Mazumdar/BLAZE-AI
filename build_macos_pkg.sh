#!/bin/bash
# Build and package the macOS application as dist/BlazeAI.pkg.
#
#   ./build_macos_pkg.sh --voice --models   # full build (recommended)
#   ./build_macos_pkg.sh                    # core UI + butler only
#
# The app is staged outside the project before signing/packaging: an
# iCloud-synced Desktop keeps re-adding FinderInfo metadata, which invalidates
# code signatures and can make Gatekeeper reject the bundle.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This package builder must run on macOS." >&2
  exit 2
fi

INCLUDE_VOICE=0
INCLUDE_MODELS=0
for ARG in "$@"; do
  case "$ARG" in
    --voice) INCLUDE_VOICE=1 ;;
    --models) INCLUDE_MODELS=1 ;;
    *) echo "Unknown option: $ARG" >&2; exit 2 ;;
  esac
done

PYTHON="${BLAZE_PYTHON:-python3}"
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

APP_PATH="$ROOT/dist/Blaze AI.app"
PKG_PATH="$ROOT/dist/BlazeAI.pkg"
VERSION="$("$VENV_PY" -c 'from blaze_ai.version import VERSION; print(VERSION)')"
if [ ! -d "$APP_PATH" ]; then
  echo "Build finished but $APP_PATH was not found." >&2
  exit 1
fi

# Ship the Prism (Ternary-Bonsai-2) runtime untouched. PyInstaller rewrites the
# install names/rpaths of Mach-O files it collects, which can make the bundled
# llama-server load the wrong libllama from llama-cpp-python. Copying the
# pristine folder into the bundle after PyInstaller keeps @executable_path
# relative loading intact; it is signed with the rest of the app below.
PRISM_SRC="$ROOT/runtimes/llama-bonsai"
PRISM_DST="$APP_PATH/Contents/Frameworks/llama-bonsai"
if [ -d "$PRISM_SRC" ]; then
  mkdir -p "$PRISM_DST"
  rsync -a --delete "$PRISM_SRC/" "$PRISM_DST/"
  echo "Bundled Prism runtime from $PRISM_SRC"
fi

# HTTPS model search/download needs the certifi CA bundle inside the app.
if ! find "$APP_PATH/Contents" -name "cacert.pem" -print -quit | grep -q .; then
  echo "WARNING: certifi cacert.pem was not bundled; model download may fail with" >&2
  echo "CERTIFICATE_VERIFY_FAILED. Reinstall requirements.txt and rebuild." >&2
fi

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/blaze-pkg.XXXXXX")"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

# Copy without resource forks/extended attributes, then ad-hoc sign locally.
ditto --norsrc --noextattr "$APP_PATH" "$STAGE/Blaze AI.app"
xattr -cr "$STAGE/Blaze AI.app"
codesign --force --deep --sign - "$STAGE/Blaze AI.app"
codesign --verify --deep --strict "$STAGE/Blaze AI.app"

rm -f "$PKG_PATH"
pkgbuild \
  --component "$STAGE/Blaze AI.app" \
  --install-location /Applications \
  --identifier ai.blaze.desktop \
  --version "$VERSION" \
  "$PKG_PATH"

echo "Built $PKG_PATH · Blaze AI $VERSION (installs Blaze AI.app into /Applications)"
