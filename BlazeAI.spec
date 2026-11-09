# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform PyInstaller build for Blaze AI.

Windows  -> dist/BlazeAI.exe            (single file, no console)
macOS    -> dist/Blaze AI.app           (unsigned .app bundle)
Linux    -> dist/BlazeAI/BlazeAI        (onedir fallback)

Run:  pyinstaller --clean --noconfirm BlazeAI.spec
"""

from pathlib import Path
import sys
import os

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)
APP_NAME = "Blaze AI"
APP_SLUG = "BlazeAI"
VERSION = "1.1.0"
BUNDLE_ID = "ai.blaze.desktop"

datas = [(str(ROOT / "blaze_ai" / "assets"), "blaze_ai/assets")]
binaries = []
hiddenimports = []

# Optional Prism (Ternary-Bonsai-2) runtime. Stock llama.cpp cannot decode
# PQ2_0/PTQ1_0 tensor types, so a compatible llama-server build is shipped
# beside the app when present.
_prism_runtime = ROOT / "runtimes" / "llama-bonsai"
if sys.platform == "darwin" and _prism_runtime.is_dir():
    datas.append((str(_prism_runtime), "llama-bonsai"))

# TLS CA bundle for Hugging Face search/download inside the frozen app.
for _package in ("certifi",):
    try:
        _datas, _binaries, _hidden = collect_all(_package)
    except Exception as exc:
        raise RuntimeError(
            "certifi is required for HTTPS model downloads; install requirements.txt"
        ) from exc
    datas += _datas
    binaries += _binaries
    hiddenimports += _hidden

# Optional voice/encoder stacks are bundled only when the build environment has
# them. The app remains fully usable without these packages.
for _package in ("faster_whisper", "ctranslate2", "sounddevice", "imageio_ffmpeg", "pynput", "pyttsx3"):
    try:
        _datas, _binaries, _hidden = collect_all(_package)
    except Exception:
        continue
    datas += _datas
    binaries += _binaries
    hiddenimports += _hidden

# Text model weights are user-downloaded, but the runtime can be bundled for
# standalone installers when explicitly requested by the build command.
if os.environ.get("BLAZE_BUNDLE_LOCAL_MODELS") == "1":
    for _package in (
        "torch",
        "transformers",
        "tokenizers",
        "safetensors",
        "huggingface_hub",
        "accelerate",
        "llama_cpp",
    ):
        try:
            _datas, _binaries, _hidden = collect_all(_package)
        except Exception as exc:
            if _package == "llama_cpp":
                # GGUF is optional; the Transformers folder path still works.
                print(f"WARNING: GGUF backend not bundled: {exc}")
                continue
            raise RuntimeError(f"Could not bundle {_package}; install requirements-local-model.txt") from exc
        datas += _datas
        binaries += _binaries
        hiddenimports += _hidden

# Qt image formats and platform plugins are handled by PyInstaller's PyQt6 hook.
a = Analysis(
    [str(ROOT / "run_blaze.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "pytest",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtQml",
        "PyQt6.QtQuick",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if sys.platform == "win32":
    _win_onedir = os.environ.get("BLAZE_WINDOWS_ONEDIR") == "1"
    if _win_onedir:
        # Onedir build for MSI packaging: installed folder contains every required
        # runtime folder/file (DLLs, assets, bundled deps, certifi CA, etc.).
        exe = EXE(
            pyz,
            a.scripts,
            [],
            exclude_binaries=True,
            name=APP_SLUG,
            debug=False,
            bootloader_ignore_signals=False,
            strip=False,
            upx=False,
            console=False,
            disable_windowed_traceback=False,
            argv_emulation=False,
            target_arch=None,
            codesign_identity=None,
            entitlements_file=None,
            icon=str(ROOT / "blaze_ai" / "assets" / "blaze_icon.ico"),
        )
        coll = COLLECT(
            exe,
            a.binaries,
            a.datas,
            strip=False,
            upx=False,
            upx_exclude=[],
            name=APP_SLUG,
        )
    else:
        exe = EXE(
            pyz,
            a.scripts,
            a.binaries,
            a.datas,
            [],
            name=APP_SLUG,
            debug=False,
            bootloader_ignore_signals=False,
            strip=False,
            upx=True,
            upx_exclude=[],
            runtime_tmpdir=None,
            console=False,
            disable_windowed_traceback=False,
            argv_emulation=False,
            target_arch=None,
            codesign_identity=None,
            entitlements_file=None,
            icon=str(ROOT / "blaze_ai" / "assets" / "blaze_icon.ico"),
        )
elif sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_SLUG,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ROOT / "blaze_ai" / "assets" / "blaze_icon.icns"),
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=APP_SLUG,
    )
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=str(ROOT / "blaze_ai" / "assets" / "blaze_icon.icns"),
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.productivity",
            "NSHighResolutionCapable": True,
            "NSMicrophoneUsageDescription": (
                "Blaze AI opens the microphone only while you hold the "
                "push-to-talk button, and transcribes locally."
            ),
            "NSScreenCaptureUsageDescription": (
                "Blaze AI records the screen only when you ask it to."
            ),
            "NSAppleEventsUsageDescription": (
                "Blaze AI uses Apple Events to open and close applications and "
                "to type text on your request."
            ),
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_SLUG,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ROOT / "blaze_ai" / "assets" / "blaze_icon.png"),
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=APP_SLUG,
    )
