"""tm_local_model — local GGUF model lifecycle (Teacher AI, build slice 1).

Load-on-demand / answer-off-thread / unload-on-idle for the embedded local
model. This slice builds ONLY the runtime lifecycle — no identity sheet, no
retrieval, no gate, no cloud escalation, and it is NOT wired into any live UI
(the Ctrl+` surface stays FAQ-only). The capability is exercised by audits /
a dev harness only.

RUNTIME PATH (decided by SPIKE_qwen_runtime, 2026-05-29):
  We wrap the OFFICIAL llama.cpp `llama-server.exe` as a subprocess and talk to
  it over localhost HTTP — we do NOT use an in-process Python binding. Two
  spike findings force this:
    1. ISA safety — the prebuilt `llama-cpp-python` CPU wheel crashed with
       0xc000001d (illegal instruction) even on the user's Zen3; the floor Celeron
       has no AVX at all. The official llama.cpp binary does RUNTIME CPU-feature
       detection, so it loads safely across CPUs (proven: it loaded on the Zen3
       where the wheel crashed).
    2. Python decoupling — the app runs on Python 3.14, which has NO
       `llama-cpp-python` wheel (the spike needed 3.12). A subprocess server
       runs the model in its OWN native process, independent of the app's
       Python version. (This module itself is stdlib-only — no psutil, no
       binding — so it imports cleanly under 3.14.)

LIFECYCLE = process lifecycle:
  load()   -> start llama-server (model resident in that process), wait healthy
  answer() -> localhost HTTP chat-completion (no reload between questions)
  unload() -> terminate the server process => its RAM is freed by the OS
  idle     -> a watchdog unloads after a (deliberately long) idle window, so a
              user asking several questions in a row doesn't pay reload cost on
              slow eMMC.

Single-flight: at most one generation at a time (a weak machine must not stack
concurrent generations or loads).
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

# ── Defaults (overridable via data/config.json; for this slice they point at
#    the kept SPIKE artifacts — bundling/installer is a later slice). ──
_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_SERVER_BINARY = r"D:\_qwen_spike\llama\llama-server.exe"
_DEFAULT_MODEL_PATH = r"D:\_qwen_spike\model\qwen2.5-1.5b-instruct-q4_k_m.gguf"
_DEFAULT_N_CTX = 2048
_DEFAULT_THREADS = 2            # potato-floor proxy; keep small
_DEFAULT_IDLE_UNLOAD_SEC = 600  # 10 min — LONG on purpose (slow-eMMC reload)
_DEFAULT_LOAD_TIMEOUT_SEC = 90  # cold load incl. slow disk
_DEFAULT_GEN_TIMEOUT_SEC = 120

# Identity sheet (slice 2): the hot-editable system prompt that makes stock
# Qwen *be* Tired Market. Re-read PER CALL (it's tiny) so editing the file
# changes behavior on the very next answer — no restart, no rebuild. That
# re-read IS the tuning loop. answer(system=...) overrides it for tests.
_IDENTITY_SHEET = "teacher_identity.md"

# Fallback only if the sheet is missing/unreadable/empty (should not happen in
# a real install; keeps the model honest rather than uninstructed).
_PLACEHOLDER_SYSTEM = (
    "You are a brief, careful built-in helper. Answer plainly. If you don't "
    "know, say so."
)


def load_identity_system() -> str:
    """Read data/internal/teacher_identity.md fresh (per call = hot-reload).
    Returns its text, or the placeholder if missing/empty. Never raises."""
    for d in (_SCRIPT_DIR / 'data' / 'internal', Path('data') / 'internal'):
        try:
            p = d / _IDENTITY_SHEET
            if p.exists():
                txt = p.read_text(encoding='utf-8').strip()
                if txt:
                    return txt
        except Exception:
            continue
    return _PLACEHOLDER_SYSTEM


class LocalModelError(RuntimeError):
    """Raised by answer()/load() on a hard failure so the caller can fall
    back (e.g. to FAQ). answer_async routes this to its on_error callback."""


def _load_cfg() -> dict:
    """Best-effort read of the REAL engine config (data/config.json). Never
    raises; missing/unreadable -> {} and the defaults above apply."""
    for d in (_SCRIPT_DIR / 'data', Path('data')):
        try:
            p = d / 'config.json'
            if p.exists():
                return json.loads(p.read_text(encoding='utf-8')) or {}
        except Exception:
            continue
    return {}


def _no_window_kwargs() -> dict:
    """v4.14.5.30: launch the llama-server subprocess with NO visible console
    window on Windows (it was popping a stray CMD window). CREATE_NO_WINDOW +
    a hidden STARTUPINFO. Cross-platform-safe: getattr() yields no-ops off
    Windows, so this returns {} there."""
    kw: dict = {}
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    if flags:
        kw['creationflags'] = flags
    si_cls = getattr(subprocess, 'STARTUPINFO', None)
    if si_cls is not None:
        try:
            si = si_cls()
            si.dwFlags |= getattr(subprocess, 'STARTF_USESHOWWINDOW', 0)
            si.wShowWindow = getattr(subprocess, 'SW_HIDE', 0)
            kw['startupinfo'] = si
        except Exception:
            pass
    return kw


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


class LocalModelRuntime:
    """One embedded-model lifecycle. Not loaded at construction — load()
    starts the server process; unload() kills it. Thread-safe; single-flight
    on both load and generation."""

    def __init__(self,
                 server_binary: Optional[str] = None,
                 model_path: Optional[str] = None,
                 n_ctx: Optional[int] = None,
                 threads: Optional[int] = None,
                 idle_unload_sec: Optional[float] = None):
        cfg = _load_cfg()
        self.server_binary = str(
            server_binary or cfg.get('local_model_server_binary')
            or _DEFAULT_SERVER_BINARY)
        self.model_path = str(
            model_path or cfg.get('local_model_path') or _DEFAULT_MODEL_PATH)
        self.n_ctx = int(n_ctx or cfg.get('local_model_n_ctx')
                         or _DEFAULT_N_CTX)
        self.threads = int(threads or cfg.get('local_model_threads')
                           or _DEFAULT_THREADS)
        self.idle_unload_sec = float(
            idle_unload_sec if idle_unload_sec is not None
            else cfg.get('local_model_idle_unload_sec',
                         _DEFAULT_IDLE_UNLOAD_SEC))

        self._proc: Optional[subprocess.Popen] = None
        self._base_url: Optional[str] = None
        self._lock = threading.RLock()       # guards proc/base_url state
        self._gen_lock = threading.Lock()    # single-flight: one gen at a time
        self._last_activity = 0.0
        self._watch_stop: Optional[threading.Event] = None
        self._watch_thread: Optional[threading.Thread] = None

    # ── state ──
    def is_loaded(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def pid(self) -> Optional[int]:
        with self._lock:
            return self._proc.pid if (self._proc is not None) else None

    # ── load ──
    def load(self, timeout: float = _DEFAULT_LOAD_TIMEOUT_SEC) -> bool:
        """Start the server (model resident) and wait until /health is ok.
        Idempotent — returns True immediately if already loaded. Raises
        LocalModelError if the binary/model is missing or never goes healthy.
        """
        with self._lock:
            if self.is_loaded():
                self._last_activity = time.time()
                return True
            if not Path(self.server_binary).exists():
                raise LocalModelError(
                    f"server binary not found: {self.server_binary}")
            if not Path(self.model_path).exists():
                raise LocalModelError(
                    f"model not found: {self.model_path}")
            port = _free_port()
            cmd = [
                self.server_binary,
                "-m", self.model_path,
                "-c", str(self.n_ctx),
                "-t", str(self.threads),
                "-ngl", "0",                 # CPU-only (deploy target has no GPU)
                "--host", "127.0.0.1",       # localhost ONLY — never exposed
                "--port", str(port),
            ]
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                    **_no_window_kwargs())  # hidden — no stray console window
            except Exception as e:
                self._proc = None
                raise LocalModelError(f"failed to start server: {e}")
            self._base_url = f"http://127.0.0.1:{port}"

        # Poll /health OUTSIDE the lock (so is_loaded/unload remain responsive).
        base = self._base_url
        deadline = time.time() + timeout
        while time.time() < deadline:
            # If the process died during startup, fail fast.
            if self._proc is None or self._proc.poll() is not None:
                self._cleanup_dead()
                raise LocalModelError("server process exited during load")
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as r:
                    if json.load(r).get("status") == "ok":
                        with self._lock:
                            self._last_activity = time.time()
                            self._start_watchdog()
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        self.unload()
        raise LocalModelError(f"server not healthy within {timeout}s")

    # ── answer (blocking; call OFF the UI thread) ──
    def answer(self, prompt: str,
               system: Optional[str] = None,
               max_tokens: int = 256,
               temperature: float = 0.3,
               timeout: float = _DEFAULT_GEN_TIMEOUT_SEC) -> str:
        """Generate an answer. BLOCKING (seconds-to-tens-of-seconds) — call
        from a worker thread, never the Tk main thread; answer_async() wraps
        this. Auto-loads if needed. Single-flight (one generation at a time).
        Raises LocalModelError on failure."""
        if not (prompt or '').strip():
            return ""
        # Single-flight: one generation at a time (a weak box must not stack
        # concurrent gens). The state lock is NOT held across the HTTP call, so
        # is_loaded()/unload()/the watchdog stay responsive during generation.
        with self._gen_lock:
            if not self.is_loaded():
                self.load()  # idempotent; takes the state lock itself
            with self._lock:
                base = self._base_url
                self._last_activity = time.time()
            # system=None -> read the identity sheet FRESH (hot-reload); an
            # explicit system= (tests) overrides it.
            sys_prompt = system if system is not None else load_identity_system()
            body = json.dumps({
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": int(max_tokens),
                "temperature": float(temperature),
                "stream": False,
            }).encode('utf-8')
            try:
                req = urllib.request.Request(
                    base + "/v1/chat/completions", data=body,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.load(r)
                txt = (data["choices"][0]["message"]["content"] or "").strip()
            except Exception as e:
                raise LocalModelError(f"generation failed: {e}")
            with self._lock:
                self._last_activity = time.time()
            return txt

    # ── answer (off-thread convenience for the eventual UI) ──
    def answer_async(self, prompt: str,
                     on_result: Callable[[str], None],
                     system: Optional[str] = None,
                     on_error: Optional[Callable[[Exception], None]] = None,
                     **kw) -> threading.Thread:
        """Run answer() on a daemon thread and deliver via on_result /
        on_error. Returns immediately (the thread). This is the off-UI-thread
        entry the Ctrl+` surface will use in a later slice."""
        def _work():
            try:
                txt = self.answer(prompt, system=system, **kw)
                on_result(txt)
            except Exception as e:
                if on_error is not None:
                    on_error(e)
        th = threading.Thread(target=_work, daemon=True,
                              name='local-model-answer')
        th.start()
        return th

    # ── unload ──
    def unload(self) -> None:
        """Terminate the server process — the OS frees its RAM. Idempotent,
        never raises."""
        with self._lock:
            self._stop_watchdog()
            proc = self._proc
            self._proc = None
            self._base_url = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ── idle watchdog ──
    def _start_watchdog(self) -> None:
        # caller holds the lock
        if self._watch_thread is not None and self._watch_thread.is_alive():
            return
        if not self.idle_unload_sec or self.idle_unload_sec <= 0:
            return
        self._watch_stop = threading.Event()
        stop = self._watch_stop

        def _watch():
            # check every ~15s; unload once idle longer than the window
            while not stop.wait(15):
                with self._lock:
                    if not self.is_loaded():
                        return
                    idle = time.time() - self._last_activity
                if idle >= self.idle_unload_sec:
                    self.unload()
                    return
        self._watch_thread = threading.Thread(
            target=_watch, daemon=True, name='local-model-idle')
        self._watch_thread.start()

    def _stop_watchdog(self) -> None:
        # caller holds the lock
        if self._watch_stop is not None:
            self._watch_stop.set()
        self._watch_thread = None

    def _cleanup_dead(self) -> None:
        with self._lock:
            self._stop_watchdog()
            self._proc = None
            self._base_url = None


# ── module-level singleton (one model per app) ──
_runtime: Optional[LocalModelRuntime] = None
_runtime_lock = threading.Lock()


def get_runtime(**kw) -> LocalModelRuntime:
    """Shared single runtime instance. kw only used on first construction."""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = LocalModelRuntime(**kw)
        return _runtime
