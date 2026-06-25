# -*- mode: python ; coding: utf-8 -*-
# ============================================================================
# Tired Market  v4.14.6.109  — PyInstaller spec (DRAFT — author Path B)
# ============================================================================
# DO NOT consider this final until the ASSET INVENTORY in the build report is
# confirmed. A missing bundled asset = a 109 that launches but fails QUIETLY
# (the exact 108 trap: wrong bundled filename -> silent 113-ticker fallback).
#
# Two build targets from ONE spec, selected by env var TM_BUILD_MODE:
#   onefile  ->  dist/TiredMarket-AllInOne-v4.14.6.109.exe   (single .exe)
#   onedir   ->  dist/TiredMarket-portable-v4.14.6.109/       (folder; we zip it)
#
# Build commands (NOT run yet — this prompt stops at draft):
#   set TM_BUILD_MODE=onefile && pyinstaller --noconfirm --clean tiredmarket_109.spec
#   set TM_BUILD_MODE=onedir  && pyinstaller --noconfirm --clean tiredmarket_109.spec
#
# The Setup installer (TiredMarket-Setup-v4.14.6.109.exe) is authored
# SEPARATELY and just wraps the onedir output — neither Inno Setup (iscc) nor
# NSIS (makensis) is installed yet (see report Step 5).
# ============================================================================

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

BUILD_MODE = os.environ.get("TM_BUILD_MODE", "onefile").strip().lower()
VERSION = "4.14.6.109"
ICON = "tired_market.ico"

# ---------------------------------------------------------------------------
# BUNDLED READ-ONLY ASSETS  (source -> dest INSIDE the bundle / _MEIPASS)
# ---------------------------------------------------------------------------
# Bundle dir layout mirrors the on-disk tree so get_app_asset_dir() (== _MEIPASS
# when frozen, then "/data") and the teacher loaders (_MEIPASS/data/internal)
# resolve correctly. dest is RELATIVE to the bundle root.
#
# ASSET-DIR READERS (these WORK once bundled here):
#   * tm_discover._bundled_snapshot_tickers -> get_app_asset_dir()/universe_iwv.json
#   * tm_teacher_brain._internal_dir        -> get_app_asset_dir()/internal/*
#   * tm_teacher_intercept._data_dir        -> get_app_asset_dir()/internal/*
#   * tm_teacher_retrieval._internal        -> _MEIPASS/data/internal/*
#
# WRITABLE-DIR READERS (bundling alone does NOT surface these on a fresh frozen
# install — see the SEED note in the report; they read get_data_dir()/USER_DATA_DIR
# which is an EMPTY C:\TiredMarket\data on first run). Bundled here anyway so a
# first-run seed step (recommended) has a source to copy from:
#   * model_registry.default.json   (tm_api_providers._resolve_model_registry)
#   * data_providers.json           (tired_market USER_DATA_DIR/"data_providers.json")
#   * provider_signup_specs.json    (tired_market INTERNAL_DIR/... )
#   * *.example.json                (templates referenced by tm_config_advisor)
datas = [
    (ICON, "."),

    # --- critical: full discovery universe (silent 113-ticker fallback if missing)
    ("data/universe_iwv.json", "data"),

    # --- internal/ teacher + recovery assets (bundle-aware readers)
    ("data/internal/faq.json", "data/internal"),
    ("data/internal/features.json", "data/internal"),
    ("data/internal/error_recovery_playbook.json", "data/internal"),
    ("data/internal/provider_signup_specs.json", "data/internal"),
    ("data/internal/teacher_getconnected.json", "data/internal"),
    ("data/internal/teacher_identity.md", "data/internal"),
    ("data/internal/teacher_ask_disclaimer.txt", "data/internal"),

    # --- ship defaults / templates (read from writable dir today -> need seed)
    ("data/model_registry.default.json", "data"),
    ("data/data_providers.json", "data"),
    ("data/api_providers.example.json", "data"),
    ("data/config.example.json", "data"),

    # --- Tk window icons (small; app may set iconphoto from these). Harmless if unused.
    ("icon_16.png", "."), ("icon_20.png", "."), ("icon_24.png", "."),
    ("icon_32.png", "."), ("icon_40.png", "."), ("icon_48.png", "."),
    ("icon_64.png", "."), ("icon_128.png", "."), ("icon_256.png", "."),
]

# Third-party packages that ship their own data files (e.g. VADER lexicon).
datas += collect_data_files("vaderSentiment")

# ---------------------------------------------------------------------------
# HIDDEN IMPORTS
# ---------------------------------------------------------------------------
# requirements.txt: yfinance, pandas, numpy, scikit-learn, beautifulsoup4,
# openpyxl, matplotlib, plyer, requests, vaderSentiment, scipy.
# Most have PyInstaller hooks, but sklearn/scipy submodules and plyer's
# platform backend are commonly missed. The app also does dynamic imports:
#   tm_scheduler:  importlib.import_module('tired_market')   (literal)
#   tm_lane_pacing: importlib.import_module(mod_name)         (VARIABLE - see report)
# All local tm_*.py modules are collected explicitly so a variable-name
# importlib call can't drop one.
hiddenimports = []
hiddenimports += collect_submodules("sklearn")
hiddenimports += collect_submodules("scipy")
hiddenimports += [
    "plyer.platforms.win.notification",   # Windows toast backend (lazy-loaded)
    "vaderSentiment.vaderSentiment",
    "bs4", "openpyxl", "yfinance", "requests",
    "pandas", "numpy",
]
# Local first-party modules (defensive against variable-name importlib)
hiddenimports += [os.path.splitext(f)[0] for f in os.listdir(".")
                  if f.startswith("tm_") and f.endswith(".py")]

block_cipher = None

a = Analysis(
    ["tired_market.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim weight: dev/test harnesses must NEVER be pulled into the bundle.
    excludes=["pytest", "_pytest", "PyQt5", "PyQt6", "PySide2", "PySide6"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if BUILD_MODE == "onefile":
    # AllInOne single .exe
    exe = EXE(
        pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
        name="TiredMarket-AllInOne-v%s" % VERSION,
        debug=False, bootloader_ignore_signals=False, strip=False,
        upx=False, upx_exclude=[], runtime_tmpdir=None,
        console=False,          # --windowed
        disable_windowed_traceback=False,
        icon=ICON,
    )
else:
    # Portable onedir: dist/TiredMarket-portable-v4.14.6.109/  (we zip the folder)
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="TiredMarket",     # exe inside the portable folder
        debug=False, bootloader_ignore_signals=False, strip=False,
        upx=False, console=False, disable_windowed_traceback=False,
        icon=ICON,
    )
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        strip=False, upx=False, upx_exclude=[],
        name="TiredMarket-portable-v%s" % VERSION,
    )
