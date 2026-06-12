r"""tm_game_detector.py
v4.13.52: Detect installed games on Windows by scanning launcher
metadata. Currently supports:
  - Steam (libraryfolders.vdf + appmanifest_*.acf for all libraries)
  - Epic Games Store (.item JSON manifests)

Output: list of (display_name, exe_basename) tuples. The exe basename
is what Tired Market matches against tasklist.exe output for the
"auto-switch to API when game running" feature in Hybrid mode.

Future launchers worth adding (not in v1):
  - GOG Galaxy   (sqlite db at %ProgramData%\\GOG.com\\Galaxy\\storage)
  - EA App       (LocalContent registry under HKLM\SOFTWARE\EA Games)
  - Battle.net   (parses Battle.net.config for installed games)
  - Ubisoft      (Connect launcher's settings.yaml)
  - Microsoft Store / Xbox Game Pass (WindowsApps + AppX manifest -- needs admin)
  - Standalone installs (heuristic Program Files scan -- noisy)
"""
import json
import re
from pathlib import Path


def _parse_vdf_minimal(text):
    """Minimal VDF tokenizer/parser.

    Steam's libraryfolders.vdf and appmanifest_*.acf use Valve's KeyValues
    format -- a tree of "key" "value" pairs and "key" { ... } blocks. This
    is enough of a parser to extract what we need; it does NOT handle
    macros, conditionals, or the binary VDF variant.

    Returns nested dict on success, empty dict on failure.
    """
    try:
        result = {}
        stack = [result]
        # Tokenize: quoted strings OR braces (preserves order)
        pattern = re.compile(r'"((?:\\.|[^"\\])*)"|(\{|\})', re.DOTALL)
        tokens = pattern.findall(text)
        i = 0
        pending_key = None
        while i < len(tokens):
            s, brace = tokens[i]
            if brace == '{':
                # Open block under pending_key
                if pending_key is not None:
                    new_dict = {}
                    stack[-1][pending_key] = new_dict
                    stack.append(new_dict)
                    pending_key = None
                i += 1
            elif brace == '}':
                if len(stack) > 1:
                    stack.pop()
                pending_key = None
                i += 1
            else:
                # Quoted string
                if pending_key is None:
                    pending_key = s
                else:
                    stack[-1][pending_key] = s
                    pending_key = None
                i += 1
        return result
    except Exception:
        return {}


# Patterns that signal a .exe is NOT the main game executable.
# These suffixes/words appear in installer, crash reporter, redist,
# and helper executables, NOT in the actual game runner.
_EXE_BLACKLIST = (
    'crash', 'setup', 'install', 'unins', 'redist',
    'vcredist', 'vc_redist', 'directx', 'dxsetup',
    'updater', 'launcher_helper', 'helper', 'config',
    'configure', 'tool', 'launchprep', 'eossdk',
    'easyanticheat', 'battleye', 'be_service',
    'cleanup', 'unitycrashhandler', 'ueprereqsetup',
    'dotnetfx', 'directxsetup', 'd3dx', 'oalinst',
    'pbsvc', 'pbsetup', 'reportcrash',
)


def _is_blacklisted_exe(exe_name):
    n = exe_name.lower()
    return any(b in n for b in _EXE_BLACKLIST)


def _guess_main_exe(game_dir):
    """Pick the most likely main executable in a game install directory.

    Heuristic: largest .exe file (up to 3 levels deep) whose filename
    doesn't match common helper-tool patterns. This is deliberately
    forgiving -- false positives are easy to remove from the textarea
    after detection, but missing a game requires user intervention.

    Returns just the basename (e.g. 'csgo.exe'), not the full path,
    since tasklist.exe matching uses basename only.

    Returns None if nothing usable was found.
    """
    if not game_dir or not game_dir.exists():
        return None

    best_name = None
    best_size = 0

    try:
        # Collect candidates from depth 0, 1, 2
        candidates = []
        try:
            candidates.extend(game_dir.glob('*.exe'))
        except Exception:
            pass
        try:
            candidates.extend(game_dir.glob('*/*.exe'))
        except Exception:
            pass
        try:
            candidates.extend(game_dir.glob('*/*/*.exe'))
        except Exception:
            pass

        for exe in candidates:
            try:
                if not exe.is_file():
                    continue
                if _is_blacklisted_exe(exe.name):
                    continue
                size = exe.stat().st_size
                # Skip tiny .exe files (almost certainly not games)
                if size < 100_000:  # 100 KB
                    continue
                if size > best_size:
                    best_size = size
                    best_name = exe.name
            except Exception:
                continue
    except Exception:
        return None

    return best_name


def _find_steam_root():
    """Return the Steam install path, or None if not found.

    v4.13.52 (initial): used registry + a few hardcoded C:/D: paths.
    v4.13.52 (revised):  also enumerates ALL drive letters (A-Z) and
    probes common Steam install patterns on each. Catches users who
    installed Steam on E:, F:, etc. without relying on the registry.

    Detection order (first match wins):
      1. HKEY_CURRENT_USER\\Software\\Valve\\Steam SteamPath
      2. HKEY_LOCAL_MACHINE\\SOFTWARE\\WOW6432Node\\Valve\\Steam InstallPath
      3. <drive>:\\Program Files (x86)\\Steam     (every drive)
      4. <drive>:\\Program Files\\Steam            (every drive)
      5. <drive>:\\Steam                            (every drive)
      6. <drive>:\\SteamLibrary                     (every drive)
      7. <drive>:\\Games\\Steam                     (every drive)
    """
    candidates = []

    # 1+2: Registry lookups (most reliable when they work)
    try:
        import winreg
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam')
            install_path, _ = winreg.QueryValueEx(key, 'SteamPath')
            winreg.CloseKey(key)
            candidates.append(Path(install_path))
        except Exception:
            pass
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r'SOFTWARE\WOW6432Node\Valve\Steam')
            install_path, _ = winreg.QueryValueEx(key, 'InstallPath')
            winreg.CloseKey(key)
            candidates.append(Path(install_path))
        except Exception:
            pass
    except Exception:
        pass

    # 3-7: Enumerate all drive letters and probe common patterns.
    # Path.exists() is fast on missing drives (returns False instantly)
    # so this loop is cheap even on systems with many drives.
    common_subpaths = [
        r'Program Files (x86)\Steam',
        r'Program Files\Steam',
        r'Steam',
        r'SteamLibrary',
        r'Games\Steam',
    ]
    import string
    for letter in string.ascii_uppercase:
        drive_root = Path(f'{letter}:\\')
        try:
            if not drive_root.exists():
                continue
        except Exception:
            continue
        for sub in common_subpaths:
            candidates.append(drive_root / sub)

    # Validate candidates: the first one with steamapps/ wins.
    for c in candidates:
        try:
            if c.exists() and (c / 'steamapps').exists():
                return c
        except Exception:
            continue
    return None


def detect_steam_games(steam_root=None, log_fn=None):
    """Scan all Steam libraries for installed games.

    Args:
        steam_root: Optional override for Steam install path.
        log_fn: Optional callable(msg, tag) for diagnostic output.

    Returns: list of (game_name, exe_basename) tuples. Empty list if
    Steam isn't installed or scan fails.
    """
    results = []

    if steam_root is None:
        steam_root = _find_steam_root()
    if steam_root is None:
        if log_fn:
            log_fn('  Steam not found.', 'muted')
        return []

    if log_fn:
        log_fn(f'  Steam root: {steam_root}', 'muted')

    # Find all libraries via libraryfolders.vdf
    libraries = []
    lf_path = steam_root / 'steamapps' / 'libraryfolders.vdf'
    if lf_path.exists():
        try:
            text = lf_path.read_text(encoding='utf-8', errors='ignore')
            vdf = _parse_vdf_minimal(text)
            lf = vdf.get('libraryfolders', {})
            for key, val in lf.items():
                if isinstance(val, dict):
                    p = val.get('path')
                    if p:
                        # VDF escapes backslashes as \\ -- normalize
                        p = p.replace('\\\\', '\\')
                        libraries.append(Path(p))
        except Exception as e:
            if log_fn:
                log_fn(f'  libraryfolders.vdf parse failed: {e}',
                       'amber')

    # Always include the main Steam library as a fallback
    if steam_root not in libraries:
        libraries.insert(0, steam_root)

    # Scan each library for appmanifest_*.acf
    seen_appids = set()
    for lib in libraries:
        steamapps = lib / 'steamapps'
        common = steamapps / 'common'
        if not steamapps.exists():
            continue
        if log_fn:
            log_fn(f'  Scanning library: {lib}', 'muted')
        try:
            acf_files = list(steamapps.glob('appmanifest_*.acf'))
        except Exception:
            continue

        for acf in acf_files:
            try:
                text = acf.read_text(encoding='utf-8', errors='ignore')
                data = _parse_vdf_minimal(text)
                appstate = data.get('AppState', {})
                appid = appstate.get('appid', '')
                # Skip duplicates (game might appear in multiple libs)
                if appid and appid in seen_appids:
                    continue
                seen_appids.add(appid)

                name = appstate.get('name', '?').strip()
                installdir = appstate.get('installdir', '').strip()
                if not installdir:
                    continue

                game_dir = common / installdir
                if not game_dir.exists():
                    continue

                exe_name = _guess_main_exe(game_dir)
                if exe_name:
                    results.append((name, exe_name))
            except Exception:
                continue

    return results


def detect_epic_games(manifest_dir=None, log_fn=None):
    """Scan Epic Games launcher manifests for installed games.

    Args:
        manifest_dir: Optional override for manifest directory path.
        log_fn: Optional callable(msg, tag) for diagnostic output.

    Returns: list of (game_name, exe_basename) tuples.
    """
    results = []

    if manifest_dir is None:
        manifest_dir = Path(
            r'C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests')

    if not manifest_dir.exists():
        if log_fn:
            log_fn('  Epic Games not found.', 'muted')
        return []

    if log_fn:
        log_fn(f'  Epic manifests: {manifest_dir}', 'muted')

    try:
        items = list(manifest_dir.glob('*.item'))
    except Exception as e:
        if log_fn:
            log_fn(f'  Failed to list Epic manifests: {e}', 'amber')
        return []

    for item in items:
        try:
            text = item.read_text(encoding='utf-8', errors='ignore')
            data = json.loads(text)
            name = (data.get('DisplayName')
                    or data.get('AppName') or '?').strip()
            launch_exe = (data.get('LaunchExecutable') or '').strip()
            if not launch_exe:
                continue
            # Get just the basename
            exe_name = launch_exe.replace('\\', '/').split('/')[-1]
            if exe_name and not _is_blacklisted_exe(exe_name):
                results.append((name, exe_name))
        except Exception:
            continue

    return results


def detect_all_games(log_fn=None):
    """Run all detectors and return a deduplicated, sorted list.

    Dedup is by lowercase exe basename. Sort is by game display name.

    Args:
        log_fn: Optional callable(msg, tag) for diagnostic output.

    Returns: list of (game_name, exe_basename) tuples.
    """
    all_results = []
    seen_exes = set()

    detectors = [
        ('Steam', detect_steam_games),
        ('Epic', detect_epic_games),
    ]

    for label, fn in detectors:
        try:
            if log_fn:
                log_fn(f'Detecting {label} games...', 'muted')
            found = fn(log_fn=log_fn)
            new_count = 0
            for name, exe in found:
                exe_key = exe.lower()
                if exe_key in seen_exes:
                    continue
                seen_exes.add(exe_key)
                all_results.append((name, exe))
                new_count += 1
            if log_fn:
                log_fn(f'  {label}: found {new_count} game(s).',
                       'muted')
        except Exception as e:
            if log_fn:
                log_fn(f'  {label} detector crashed: {e}', 'amber')

    all_results.sort(key=lambda x: x[0].lower())
    return all_results
