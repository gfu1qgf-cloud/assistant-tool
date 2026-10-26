# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

from comtypes.client import GetModule


sys.setrecursionlimit(max(sys.getrecursionlimit(), 5000))

project_root = Path(SPECPATH).parent
datas = []
binaries = []
hiddenimports = []

app_icon = project_root / "app_icon.ico"
if not app_icon.is_file():
    raise SystemExit("Missing application icon: app_icon.ico")
for icon_name in ("app_icon.ico", "app_icon.png"):
    datas.append((str(project_root / icon_name), "."))

task_table_template = project_root / "任务登记表格.ods"
if task_table_template.is_file():
    datas.append((str(task_table_template), "."))

waste_data = project_root / "app_plugins" / "builtin" / "waste_reminder" / "data"
for data_file in waste_data.rglob("*"):
    if data_file.is_file():
        destination = data_file.parent.relative_to(project_root).as_posix()
        datas.append((str(data_file), destination))

# Preserve all real Poppins weights (including italic) and their OFL notice.
font_data = project_root / "app_plugins" / "builtin" / "batch_text_video" / "fonts" / "poppins"
for style in ("Thin", "ThinItalic", "ExtraLight", "ExtraLightItalic", "Light", "LightItalic",
              "Regular", "Italic", "Medium", "MediumItalic", "SemiBold", "SemiBoldItalic",
              "Bold", "BoldItalic", "ExtraBold", "ExtraBoldItalic", "Black", "BlackItalic"):
    if not (font_data / ("Poppins-" + style + ".ttf")).is_file():
        raise SystemExit("Missing bundled Poppins font: " + style)
for notice in ("OFL.txt", "METADATA.pb", "SOURCE.txt"):
    if not (font_data / notice).is_file():
        raise SystemExit("Missing bundled font notice: " + notice)
for data_file in font_data.iterdir():
    if data_file.is_file() and data_file.suffix.lower() in {".ttf", ".txt", ".pb"}:
        datas.append((str(data_file), data_file.parent.relative_to(project_root).as_posix()))

# Open Sans variable axes include all 300–800 weights, upright and italic.
open_sans_data = project_root / "app_plugins" / "builtin" / "batch_text_video" / "fonts" / "opensans"
for resource in ("OpenSans[wdth,wght].ttf", "OpenSans-Italic[wdth,wght].ttf", "OFL.txt", "METADATA.pb", "SOURCE.txt"):
    data_file = open_sans_data / resource
    if not data_file.is_file():
        raise SystemExit("Missing bundled Open Sans resource: " + resource)
    datas.append((str(data_file), data_file.parent.relative_to(project_root).as_posix()))

mpv_runtime = project_root / "runtime" / "mpv"
required_mpv_files = ("mpv.exe", "vulkan-1.dll")
missing_mpv_files = [
    name for name in required_mpv_files
    if not (mpv_runtime / name).is_file()
]
if missing_mpv_files:
    raise SystemExit(
        "Missing pinned mpv runtime: " + ", ".join(missing_mpv_files)
        + ". Run: python packaging/fetch_mpv.py"
    )
for runtime_name in (*required_mpv_files, "README.md"):
    runtime_file = mpv_runtime / runtime_name
    if runtime_file.is_file():
        datas.append((str(runtime_file), "runtime/mpv"))

# Bundle the standard, pinned FFmpeg processes and their original notices.
ffmpeg_runtime = project_root / "runtime" / "ffmpeg"
required_ffmpeg_files = ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe", "LICENSE", "README.txt", "BUILD_INFO.txt")
missing_ffmpeg_files = [name for name in required_ffmpeg_files if not (ffmpeg_runtime / name).is_file()]
if missing_ffmpeg_files:
    raise SystemExit("Missing pinned FFmpeg runtime: " + ", ".join(missing_ffmpeg_files)
                     + ". Run: python packaging/fetch_ffmpeg.py")
for runtime_name in required_ffmpeg_files:
    datas.append((str(ffmpeg_runtime / runtime_name), "runtime/ffmpeg"))

# Native DNxHR alpha decoder and complete source/licence provenance.
dnx_runtime = project_root / "app_plugins" / "builtin" / "batch_text_video" / "native"
for resource in ("dnx-alpha.exe", "LICENSE-APACHE.txt", "THIRD_PARTY_NOTICES.txt", "RUST-THIRD-PARTY.html", "BUILD_INFO.json"):
    if not (dnx_runtime / resource).is_file():
        raise SystemExit("Missing DNxHR alpha resource: " + resource)
for resource in dnx_runtime.rglob("*"):
    if resource.is_file() and resource.suffix in {".exe", ".txt", ".html", ".json", ".toml", ".lock", ".rs"}:
        datas.append((str(resource), resource.parent.relative_to(project_root).as_posix()))

# Generate and collect the Windows UI Automation wrapper used by the external
# Flow parameter guard. This avoids trying to create comtypes.gen files beside
# the installed executable at runtime.
GetModule("UIAutomationCore.dll")

for package_name in (
    "stable_whisper",
    "faster_whisper",
    "onnxruntime",
    "ctranslate2",
    "tokenizers",
    "av",
    "elevenlabs",
    "PIL",
    "transformers",
    "pycaw",
    "comtypes",
    "yt_dlp",
):
    package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

hiddenimports += collect_submodules("googleapiclient")
hiddenimports += collect_submodules("google_auth_oauthlib")
hiddenimports += collect_submodules("comtypes.gen")
hiddenimports += collect_submodules("app_plugins")

a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "PyQt5",
        "PyQt5_sip",
        "PySide6",
        "jupyter",
        "jupyter_client",
        "jupyter_core",
        "jupyterlab",
        "keras",
        "notebook",
        "pandas",
        "tensorboard",
        "tensorflow",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AssistantTool",
    icon=str(app_icon),
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AssistantTool",
)
