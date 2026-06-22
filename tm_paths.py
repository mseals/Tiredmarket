"""v4.14.6.105-retire-mover — single source of truth for where data lives.

RESOLVER FOUNDATION (Stage 1) — RETAINED. Two resolvers:

  get_data_dir()       -> the WRITABLE user-data directory (DBs, jsonl, logs,
                          config). Pointer-driven + frozen-aware.
  get_app_asset_dir()  -> the READ-ONLY bundled-asset directory (internal/,
                          *.bundled.json, *.example.json, default registries).
                          Ships with the code; NEVER moves.

Flag `data_dir_localappdata` (default OFF) keeps today's exact behavior
(SCRIPT_DIR/data, byte-identical to the pre-patch _get_user_data_dir). Flag ON
(and ALWAYS when frozen) routes the writable data through a pointer file at
%LOCALAPPDATA%\\TiredMarket\\datadir.txt so a one-file PyInstaller bundle never
resolves data into the wiped _MEIPASS temp dir (the .exe data-loss bug).

RETIRED (v4.14.6.105): the runtime data-RELOCATE / MOVE feature (v4.14.6.101–
v4.14.6.104 Stages 2–4) has been removed. It was a persistent source of bugs and
Mike does not need runtime relocation — the app just needs to reliably FIND its
data and not lose it in a frozen .exe, which the resolver above does. Data
location is now decided at RESOLVE-TIME ONLY: the flag-governed default when there
is no pointer, or a committed pointer if one exists. There is no runtime move,
no two-phase commit, no move marker, and no staging sweep. The pointer READ path
and v104 precedence (valid pointer honored; dead pointer handled gracefully —
never silent-empty; flag governs only the no-pointer default) are retained: a
future first-run-choice or installer can still write a pointer for the resolver
to honor. clear_cache() is retained as part of the resolver API.

stdlib-only — safe to import from every module (no import cycle). The resolved
dir is memoized after first use, so the lazy path accessors stay cheap.
"""
from __future__ import annotations

import os
import sys
import json
from pathlib import Path

_APP_NAME = "TiredMarket"
_CACHE: dict = {}


# ─── environment / location primitives ───────────────────────────────────────
def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _app_base_dir() -> Path:
    """Where the CODE lives: the bundle dir when frozen (_MEIPASS / exe dir),
    else this module's directory (== the app's SCRIPT_DIR)."""
    if _is_frozen():
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
        return Path(base)
    return Path(__file__).resolve().parent


def _legacy_data_dir() -> Path:
    """Today's location: data/ next to the app code."""
    return _app_base_dir() / "data"


def _appdata_fallback_root() -> Path:
    """The historical %APPDATA%/TiredMarket fallback (matches the pre-patch
    _get_user_data_dir except-branch exactly)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = (os.environ.get("XDG_DATA_HOME")
                or os.path.expanduser("~/.local/share"))
    return Path(base) / _APP_NAME


def _localappdata_root() -> Path:
    """%LOCALAPPDATA%\\TiredMarket (Win) / ~/.local/share/TiredMarket — the
    default ON-flag location and the fixed home of the pointer file."""
    if os.name == "nt":
        base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
                or os.path.expanduser("~"))
    else:
        base = (os.environ.get("XDG_DATA_HOME")
                or os.path.expanduser("~/.local/share"))
    return Path(base) / _APP_NAME


def _visible_default_data_dir() -> Path:
    """v4.14.6.108-standalone-prep: the VISIBLE default data location for a
    frozen/standalone install — [SystemDrive]\\TiredMarket\\data (e.g.
    C:\\TiredMarket\\data). Normally writable for a standard user (unlike
    Program Files), and visible/removable — NOT hidden in %LOCALAPPDATA%, and
    never the wiped _MEIPASS temp. Only the tiny pointer lives in appdata; the
    data lives here (or wherever the first-run chooser / a pointer names)."""
    if os.name == "nt":
        base = os.environ.get("SystemDrive", "C:") + "\\"
    else:
        base = os.path.expanduser("~")
    return Path(base) / _APP_NAME / "data"


def _pointer_path() -> Path:
    """Fixed, always-findable pointer file naming the data dir. Lives in
    %LOCALAPPDATA%\\TiredMarket even if the data itself is elsewhere — so it
    loads before any DB and survives restart / a frozen .exe."""
    return _localappdata_root() / "datadir.txt"


def _flag_on() -> bool:
    """`data_dir_localappdata`. Frozen builds are ALWAYS on (must never use the
    _MEIPASS temp). Otherwise read from the legacy config location (always
    computable from the code dir, before any DB opens); default OFF."""
    if _is_frozen():
        return True
    try:
        with open(_legacy_data_dir() / "config.json", encoding="utf-8") as f:
            return bool(json.load(f).get("data_dir_localappdata", False))
    except Exception:
        return False


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        t = p / ".write_test"
        t.write_text("ok", encoding="utf-8")
        t.unlink()
        return True
    except Exception:
        return False


def _dir_has_data(p: Path) -> bool:
    """True if p already looks like a Tired Market data dir — used to avoid
    stranding an existing install when the flag is flipped on."""
    try:
        return any((p / n).exists()
                   for n in ("cache.db", "tired_market.db", "config.json"))
    except Exception:
        return False


# ─── pointer file (read + initialize default) ────────────────────────────────
def _pointer_tmp_path() -> Path:
    pf = _pointer_path()
    return pf.parent / (pf.name + ".tmp")


def _read_pointer() -> "Path | None":
    try:
        pf = _pointer_path()
        tmpf = _pointer_tmp_path()
        # v4.14.6.101: a datadir.txt.tmp is NEVER live data. If it's an orphan
        # (no final pointer) the atomic write crashed mid-flight — discard it
        # and fall through to normal resolution (still names real data). If a
        # final pointer also exists, the .tmp is a stale leftover — discard it
        # too; the final pointer is authoritative.
        if tmpf.exists():
            try:
                tmpf.unlink()
            except Exception:
                pass
        if pf.exists():
            raw = pf.read_text(encoding="utf-8").strip()
            if raw:
                return Path(raw)
    except Exception:
        pass
    return None


def _write_pointer(target: Path) -> None:
    try:
        pf = _pointer_path()
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(str(target), encoding="utf-8")
    except Exception:
        pass


# Resolution status — the resolver records here whether it returned a healthy
# dir ('ok'), a degraded-but-usable one ('warning'), or could not find the
# configured location ('blocking'). Startup / the settings UI read this so a
# missing data dir surfaces a clear message instead of a silent empty dir.
_STATUS: dict = {"state": "ok", "message": "", "missing_path": None}


def _set_status(state: str, message: str = "", missing=None) -> None:
    _STATUS["state"] = state
    _STATUS["message"] = message
    _STATUS["missing_path"] = (str(missing) if missing is not None else None)


def get_resolution_status() -> dict:
    """{'state': 'ok'|'warning'|'blocking', 'message': str,
        'missing_path': str|None} from the last get_data_dir() resolution."""
    return dict(_STATUS)


# ─── resolvers ───────────────────────────────────────────────────────────────
def _resolve_data_dir() -> Path:
    """v4.14.6.104-pointer-precedence: a committed pointer ALWAYS wins,
    regardless of the data_dir_localappdata flag. The flag's ONLY legitimate job
    is the fresh-install DEFAULT location when there is no pointer at all.
    Precedence:
        1. valid committed pointer        -> honor it (flag irrelevant)
        2. dead pointer (target gone)     -> legacy fallback / blocking status
        3. no pointer                     -> flag-governed default
    """
    _set_status("ok")

    # ── 1/2. POINTER FIRST — regardless of flag. The pointer (datadir.txt) is
    # written only to a real intended location (fresh-install/legacy-keep
    # default below, or a future installer/first-run-choice). _read_pointer()
    # discards any orphan .tmp, so resolution always names real data.
    ptr = _read_pointer()
    if ptr is not None:
        # Case 1: a committed pointer that names an EXISTING, populated dir wins
        # unconditionally — this is the user's explicit choice, above any flag.
        if ptr.exists() and ptr.is_dir():
            if _writable(ptr):
                return ptr
            # Exists but not writable -> warn, still return it (read at least).
            _set_status("warning", f"data dir not writable: {ptr}")
            return ptr
        # Case 2: DEAD pointer — target gone (USB out, drive remap, folder
        # deleted). NEVER create the named path (silent re-creation is the
        # "all my data is gone" failure). Fall back to a populated legacy dir
        # with a warning, else surface a blocking status naming the missing
        # path so the UI can offer to re-point.
        legacy = _legacy_data_dir()
        if _dir_has_data(legacy) and _writable(legacy):
            _set_status("warning",
                        f"configured data dir not found ({ptr}); "
                        f"falling back to {legacy}")
            return legacy
        _set_status("blocking",
                    f"configured data dir not found: {ptr}", missing=ptr)
        return legacy

    # ── 3. NO POINTER — the flag's ONLY real job: the fresh-install default.
    # Flag OFF (default): byte-identical to the pre-patch _get_user_data_dir —
    # SCRIPT_DIR/data (with the historical %APPDATA% fallback if unwritable).
    # A normal, never-moved install has no pointer, so it lands here unchanged.
    if not _flag_on():
        legacy = _legacy_data_dir()
        if _writable(legacy):
            return legacy
        fb = _appdata_fallback_root()
        try:
            fb.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return fb

    # Flag ON, no pointer.
    # NOT frozen (dev with the flag flipped on): keep an existing populated
    # legacy install if present, else default to %LOCALAPPDATA% — unchanged.
    if not _is_frozen():
        legacy = _legacy_data_dir()
        if _dir_has_data(legacy) and _writable(legacy):
            _write_pointer(legacy)
            return legacy
        lad = _localappdata_root()
        if _writable(lad):
            _write_pointer(lad)
            return lad
        if _writable(legacy):
            return legacy
        return lad

    # FROZEN, no pointer. The first-run chooser (tired_market) normally writes
    # the pointer BEFORE this resolves; this is the SAFETY-NET default when the
    # chooser was bypassed. v4.14.6.108-standalone-prep: data defaults to a
    # VISIBLE location ([SystemDrive]\TiredMarket\data), NOT hidden in
    # %LOCALAPPDATA%. The visible target is recorded in the pointer. If it
    # isn't writable, degrade to %LOCALAPPDATA% (writable, safe) — NEVER to
    # _legacy_data_dir(), which when frozen is _MEIPASS\data (the wiped temp).
    target = _visible_default_data_dir()
    if _writable(target):
        _write_pointer(target)
        return target
    lad = _localappdata_root()
    if _writable(lad):
        _write_pointer(lad)
        return lad
    return target


def get_data_dir() -> Path:
    """The single source of truth for the WRITABLE user-data directory.
    Memoized after first resolution (the dir does not change mid-run)."""
    d = _CACHE.get("data")
    if d is None:
        d = _resolve_data_dir()
        _CACHE["data"] = d
    return d


def get_app_asset_dir() -> Path:
    """The READ-ONLY bundled-asset directory (ships with the code; never moves).
    Frozen-aware via _MEIPASS — this is the frozen-.exe data-loss fix."""
    return _app_base_dir() / "data"


def clear_cache() -> None:
    """Drop the memoized data dir. Retained as part of the resolver API."""
    _CACHE.clear()


# ─── data-dir reporting (read-only; used by the Settings DATA LOCATION view) ──
def _iter_files(root: Path):
    for dp, _dn, fn in os.walk(str(root)):
        for f in fn:
            yield Path(dp) / f


def _dir_size(root: Path) -> int:
    total = 0
    for p in _iter_files(root):
        try:
            total += p.stat().st_size
        except Exception:
            pass
    return total


def current_data_dir_info() -> dict:
    """For the settings UI: current resolved path + size + resolution status.
    Read-only reporter — no move/relocate side effects."""
    d = Path(get_data_dir())
    exists = d.exists()
    return {
        "path": str(d),
        "exists": exists,
        "size_bytes": (_dir_size(d) if exists else 0),
        "status": get_resolution_status(),
    }
