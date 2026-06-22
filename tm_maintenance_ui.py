"""tm_maintenance_ui — v4.14.6.28 Tk UI for the in-app Maintenance tool.

What v4.14.6.28 changes vs. v4.14.6.27:
  - Per-category visible status. Each card always shows its own state —
    `computing…` / `done — N items / ~M MB` / `done — nothing to clean`
    / `error — <short reason>` — so there's never a silent absence.
  - Collapsible cards (collapsed by default). Each card shows ONE tidy
    row by default; expanding reveals the per-subject / sample / notes
    detail. v4.14.6.27 dumped every card's full detail inline; the
    gossip card's 7,354-entry per-ticker list pushed the other 4 cards
    thousands of pixels below the scroll region. Collapsible cards fix
    that fundamentally.
  - Header progress: `Scanning… (k/N done)` → `Dry-run complete —
    N/N scanned · Total reclaimable: X items / ~Y MB`.
  - Incremental dry-run. The engine fires a progress callback per task
    completion; the dialog updates the matching card in real time
    instead of waiting for all tasks to finish.
  - Gossip-removal gated by `cfg['maintenance_enable_gossip_removal']`
    (default False). When off, the gossip card is absent (not greyed
    out), and the public ship never offers it. the user's MAIN has the
    flag True so he still sees it.

Flow (unchanged from v4.14.6.27 otherwise):
  1. Open → render N placeholder cards as `computing…`.
  2. Worker thread runs each Task's dry_run sequentially; per-task
     completion posts (task_id, manifest) → main thread updates that
     card.
  3. User ticks categories, optionally edits the excluded-ticker list,
     clicks Apply Selected → confirm dialog → engine pause / clean /
     VACUUM / resume on the background thread.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Optional

import tm_maintenance


# Disclosure-triangle glyphs (text-only — no font dependencies).
_TRI_COLLAPSED = '▶'
_TRI_EXPANDED  = '▼'


def _human_bytes(n):
    return tm_maintenance._human_bytes(n)


# ─── TaskCard — one collapsible card per Task ────────────────────────────

class _TaskCard:
    """One collapsible card. Header (always visible): triangle + title
    + status + checkbox. Detail (toggled): per_subject, sample, notes.

    Status states tracked via `set_status(state, **kwargs)`:
        'computing' - scan in progress
        'done'      - finished, has 'count' and 'bytes' (optionally
                       'sample_count' / 'subject_count')
        'empty'     - finished, nothing reclaimable
        'error'     - finished with error; pass 'message' for short reason
    """
    def __init__(self, parent: tk.Misc, task_id: str, label: str,
                 description: str, colours: dict,
                 on_check_change=None):
        self.task_id = task_id
        self.label = label
        self.description = description
        self.colours = colours
        self.var = tk.BooleanVar(value=False)
        self.expanded = False
        self.manifest = None
        self._on_check_change = on_check_change

        bg     = colours.get('bg', '#0e1116')
        fg     = colours.get('text', '#dcdfe6')
        accent = colours.get('accent', '#3aa676')
        muted  = colours.get('muted', '#7a828e')

        self.frame = tk.Frame(parent, bg=bg, padx=8, pady=6,
                               highlightbackground=muted,
                               highlightthickness=1)
        self.frame.pack(fill='x', pady=3, padx=4)

        # ── Header row (always visible) ──
        self.header = tk.Frame(self.frame, bg=bg)
        self.header.pack(fill='x')

        # Disclosure triangle (clickable).
        self.tri_lbl = tk.Label(self.header, text=_TRI_COLLAPSED,
                                  bg=bg, fg=fg, cursor='hand2',
                                  font=('Segoe UI', 9), width=2)
        self.tri_lbl.pack(side='left')
        self.tri_lbl.bind('<Button-1>', lambda e: self.toggle())

        # Checkbox (start disabled — enable once status is 'done' with
        # non-zero count or 'empty'; an erroring or computing card
        # shouldn't accept a tick).
        self.cb = tk.Checkbutton(
            self.header, variable=self.var, bg=bg, fg=fg,
            selectcolor=bg, activebackground=bg,
            activeforeground=fg, cursor='hand2', state='disabled',
            command=self._on_cb_toggle)
        self.cb.pack(side='left')

        # Title (clickable area for expand/collapse too).
        self.title_lbl = tk.Label(
            self.header, text=label, bg=bg, fg=accent,
            font=('Segoe UI', 10, 'bold'), cursor='hand2')
        self.title_lbl.pack(side='left', padx=(2, 0))
        self.title_lbl.bind('<Button-1>', lambda e: self.toggle())

        # Status (right-aligned).
        self.status_lbl = tk.Label(
            self.header, text='', bg=bg, fg=muted,
            font=('Segoe UI', 9))
        self.status_lbl.pack(side='right')

        # ── Detail body (built lazily on first expand) ──
        self.body = tk.Frame(self.frame, bg=bg)
        # NOT packed yet — only when expanded.
        self._body_built = False

        # Initial state: computing.
        self.set_status('computing')

    # ── State setters ──

    def set_status(self, state: str, count: int = 0,
                   bytes_freed: int = 0, message: str = '',
                   subject_count: int = 0, sample_count: int = 0,
                   archive_count: int = 0):
        muted  = self.colours.get('muted', '#7a828e')
        accent = self.colours.get('accent', '#3aa676')
        amber  = self.colours.get('amber', '#e0a020')
        red    = '#e05050'

        if state == 'computing':
            self.status_lbl.configure(
                text='computing…', fg=muted)
            self.cb.configure(state='disabled')
            self.var.set(False)
        elif state == 'done':
            total_items = int(count) + int(archive_count)
            parts = []
            if count: parts.append(f'{count:,} remove')
            if archive_count: parts.append(f'{archive_count} archive')
            if not parts: parts.append('nothing to clean')
            txt = (f"done — {' + '.join(parts)} / "
                   f"~{_human_bytes(bytes_freed)}")
            self.status_lbl.configure(text=txt, fg=accent)
            # Enable the checkbox so the user can tick.
            self.cb.configure(state='normal')
        elif state == 'empty':
            self.status_lbl.configure(
                text='done — nothing to clean', fg=muted)
            # Disable: nothing to do.
            self.cb.configure(state='disabled')
            self.var.set(False)
        elif state == 'error':
            short = message[:60] if message else 'scan failed'
            self.status_lbl.configure(
                text=f'error — {short}', fg=red)
            self.cb.configure(state='disabled')
            self.var.set(False)
        else:
            self.status_lbl.configure(text=state, fg=amber)

    def apply_manifest(self, m: dict):
        """Update the card from a freshly-completed manifest. Computes
        status state from the manifest's contents."""
        self.manifest = m
        if m.get('error'):
            self.set_status('error', message=m['error'])
        else:
            count = int(m.get('would_remove', 0) or 0)
            archive = int(m.get('would_archive', 0) or 0)
            bytes_freed = int(m.get('bytes_freed', 0) or 0)
            if count == 0 and archive == 0:
                self.set_status('empty')
            else:
                self.set_status(
                    'done', count=count, bytes_freed=bytes_freed,
                    archive_count=archive)
        # Rebuild body if expanded; otherwise lazy-build on next expand.
        if self.expanded:
            self._rebuild_body()

    # ── Toggle / body build ──

    def toggle(self):
        self.expanded = not self.expanded
        self.tri_lbl.configure(
            text=_TRI_EXPANDED if self.expanded else _TRI_COLLAPSED)
        if self.expanded:
            if not self._body_built or self.manifest is not None:
                self._rebuild_body()
            self.body.pack(fill='x', pady=(4, 0))
        else:
            self.body.pack_forget()

    def _rebuild_body(self):
        # Clear existing children.
        for child in list(self.body.children.values()):
            try: child.destroy()
            except Exception: pass

        bg     = self.colours.get('bg', '#0e1116')
        fg     = self.colours.get('text', '#dcdfe6')
        muted  = self.colours.get('muted', '#7a828e')
        amber  = self.colours.get('amber', '#e0a020')

        # Always show the task description.
        tk.Label(
            self.body, text=self.description, bg=bg, fg=muted,
            font=('Segoe UI', 8),
            wraplength=720, justify='left').pack(
            anchor='w', pady=(0, 4))

        m = self.manifest
        if not m:
            tk.Label(self.body, text='  (still computing…)',
                      bg=bg, fg=muted,
                      font=('Segoe UI', 8)).pack(anchor='w')
            self._body_built = True
            return

        # Per-subject breakdown (top 25 only — preserves the "show me
        # something" intent without re-creating the v4.14.6.27 layout bug
        # where a 7,354-line label exploded the scroll region).
        ps = m.get('per_subject') or {}
        if ps:
            total = len(ps)
            top_n = 25
            if total > top_n:
                tk.Label(
                    self.body,
                    text=f"  Per-subject breakdown "
                          f"(top {top_n} of {total:,}):",
                    bg=bg, fg=muted,
                    font=('Segoe UI', 8)).pack(anchor='w', pady=(4, 0))
            else:
                tk.Label(
                    self.body,
                    text=f"  Per-subject breakdown ({total}):",
                    bg=bg, fg=muted,
                    font=('Segoe UI', 8)).pack(anchor='w', pady=(4, 0))
            # Sort by count descending where the value looks numeric;
            # otherwise stable insertion order.
            try:
                items = sorted(ps.items(),
                                key=lambda kv: -int(kv[1]))[:top_n]
            except Exception:
                items = list(ps.items())[:top_n]
            lines = '\n'.join(f'    · {k}: {v}' for k, v in items)
            tk.Label(self.body, text=lines, bg=bg, fg=fg,
                     font=('Consolas', 8), justify='left').pack(
                anchor='w')

        # Notes (amber).
        for note in (m.get('notes') or []):
            tk.Label(
                self.body, text=f"  ! {note}", bg=bg, fg=amber,
                font=('Segoe UI', 8),
                wraplength=720, justify='left').pack(
                anchor='w', pady=(2, 0))

        # Sample lines (cap visible at 12; manifest log captures all).
        sample = m.get('sample') or []
        if sample:
            shown = sample[:12]
            tk.Label(
                self.body,
                text=f"  Sample ({len(shown)} of {len(sample)}):",
                bg=bg, fg=muted,
                font=('Segoe UI', 8)).pack(anchor='w', pady=(4, 0))
            stxt = '\n'.join(f"    {s}" for s in shown)
            tk.Label(self.body, text=stxt, bg=bg, fg=fg,
                     font=('Consolas', 8), justify='left').pack(
                anchor='w')

        self._body_built = True

    # ── Checkbox callback (forwards to parent) ──

    def _on_cb_toggle(self):
        if self._on_check_change:
            try: self._on_check_change()
            except Exception: pass


# ─── MaintenanceWindow ───────────────────────────────────────────────────

class MaintenanceWindow:
    """Toplevel maintenance dialog."""

    def __init__(self, root: tk.Misc, app):
        self.root = root
        self.app = app
        # v4.14.6.28: engine inherits cfg-gated registry; gossip_removal
        # only present when cfg['maintenance_enable_gossip_removal'] is
        # True. the user's MAIN sets it True; PUBLIC ships at default False
        # so other users don't see the gossip card.
        self.engine = tm_maintenance.MaintenanceEngine(
            app, log_fn=self._log)
        self._excluded_tickers: set = set()
        self._cards: dict = {}            # task_id -> _TaskCard
        self._progress_q: queue.Queue = queue.Queue()
        self._busy = False
        self._total_tasks = len(self.engine.tasks)
        self._completed_tasks = 0

        self.win = tk.Toplevel(root)
        self.win.title('Maintenance — Tired Market')
        self.win.geometry('820x640')
        self.win.minsize(720, 520)
        try:
            self.colours = getattr(app, 'c', {})
        except Exception:
            self.colours = {}
        bg = self.colours.get('bg', '#0e1116')
        self.win.configure(bg=bg)

        self._build_ui()
        self._render_placeholder_cards()
        # Kick off dry-run immediately on open.
        self.win.after(50, self._start_dry_run)
        # Tail the progress queue.
        self.win.after(150, self._drain_progress_q)

    # ─── UI build ───────────────────────────────────────────────────

    def _build_ui(self):
        bg     = self.colours.get('bg', '#0e1116')
        fg     = self.colours.get('text', '#dcdfe6')
        accent = self.colours.get('accent', '#3aa676')

        header = tk.Frame(self.win, bg=bg, padx=12, pady=8)
        header.pack(fill='x')
        tk.Label(header, text='Maintenance', bg=bg, fg=accent,
                 font=('Segoe UI', 14, 'bold')).pack(side='left')
        self.summary_lbl = tk.Label(
            header, text=self._header_text_initial(),
            bg=bg, fg=fg, font=('Segoe UI', 9))
        self.summary_lbl.pack(side='right')

        intro = tk.Label(
            self.win,
            text=("Dry-run first, always. Click the ▶ on a row to "
                  "expand its detail. Tick the rows to clean, then "
                  "Apply. The app pauses background tasks during the "
                  "clean and resumes after — no restart needed."),
            bg=bg, fg=fg, font=('Segoe UI', 8),
            wraplength=780, justify='left', padx=12)
        intro.pack(fill='x', pady=(0, 8))

        # Categories area — scrollable.
        cats_frame = tk.Frame(self.win, bg=bg)
        cats_frame.pack(fill='both', expand=True, padx=12, pady=4)
        self._cats_canvas = tk.Canvas(cats_frame, bg=bg,
                                       highlightthickness=0)
        scr = ttk.Scrollbar(cats_frame, orient='vertical',
                             command=self._cats_canvas.yview)
        self._cats_inner = tk.Frame(self._cats_canvas, bg=bg)
        self._cats_inner.bind(
            '<Configure>',
            lambda e: self._cats_canvas.configure(
                scrollregion=self._cats_canvas.bbox('all')))
        self._cats_canvas.create_window((0, 0), window=self._cats_inner,
                                         anchor='nw')
        self._cats_canvas.configure(yscrollcommand=scr.set)
        self._cats_canvas.pack(side='left', fill='both', expand=True)
        scr.pack(side='right', fill='y')

        # Buttons.
        btns = tk.Frame(self.win, bg=bg, padx=12, pady=10)
        btns.pack(fill='x')
        self.refresh_btn = tk.Button(
            btns, text='Refresh dry-run', bg=bg, fg=fg,
            relief='flat', cursor='hand2',
            command=self._start_dry_run)
        self.refresh_btn.pack(side='left')
        self.excl_btn = tk.Button(
            btns, text='Edit excluded tickers…', bg=bg, fg=fg,
            relief='flat', cursor='hand2',
            command=self._edit_excluded)
        # Only useful when gossip_removal is registered.
        if not any(t.id == 'gossip_removal' for t in self.engine.tasks):
            self.excl_btn.configure(state='disabled')
        self.excl_btn.pack(side='left', padx=(8, 0))
        self.apply_btn = tk.Button(
            btns, text='Apply Selected', bg=accent, fg=bg,
            relief='flat', cursor='hand2',
            font=('Segoe UI', 9, 'bold'),
            state='disabled', command=self._on_apply)
        self.apply_btn.pack(side='right')
        self.close_btn = tk.Button(
            btns, text='Close', bg=bg, fg=fg,
            relief='flat', cursor='hand2',
            command=self.win.destroy)
        self.close_btn.pack(side='right', padx=(0, 8))

        # Run-log area.
        outframe = tk.Frame(self.win, bg=bg)
        outframe.pack(fill='x', padx=12, pady=(0, 12))
        tk.Label(outframe, text='Run log:', bg=bg, fg=fg,
                 font=('Segoe UI', 8, 'bold')).pack(anchor='w')
        self.out = scrolledtext.ScrolledText(
            outframe, height=8, bg='#181d23', fg=fg,
            font=('Consolas', 8), insertbackground=fg)
        self.out.pack(fill='x')
        self.out.configure(state='disabled')

    def _header_text_initial(self) -> str:
        return f'Scanning… (0/{self._total_tasks} categories done)'

    def _header_text_progress(self) -> str:
        return (f'Scanning… ({self._completed_tasks}/'
                f'{self._total_tasks} categories done)')

    def _header_text_done(self, total_rows: int, total_bytes: int) -> str:
        return (f'Dry-run complete — {self._total_tasks}/'
                f'{self._total_tasks} scanned · '
                f'Total reclaimable: {total_rows:,} items / '
                f'~{_human_bytes(total_bytes)}')

    def _render_placeholder_cards(self):
        """Create one card per task immediately, all showing computing…
        so the user can see which categories exist and what their state
        is, even before any scan completes."""
        for child in list(self._cats_inner.children.values()):
            try: child.destroy()
            except Exception: pass
        self._cards.clear()
        for t in self.engine.tasks:
            card = _TaskCard(
                self._cats_inner, t.id, t.label, t.description,
                self.colours, on_check_change=self._refresh_apply_state)
            self._cards[t.id] = card

    # ─── Helpers ────────────────────────────────────────────────────

    def _log(self, msg: str):
        try:
            self._progress_q.put(('log', msg))
        except Exception:
            pass

    def _post_task_done(self, task_id: str, manifest: dict):
        """Engine progress callback (worker thread). Just put on queue
        for the UI thread to apply."""
        try:
            self._progress_q.put(('task', task_id, manifest))
        except Exception:
            pass

    def _drain_progress_q(self):
        try:
            while True:
                evt = self._progress_q.get_nowait()
                kind = evt[0]
                if kind == 'log':
                    msg = evt[1]
                    self.out.configure(state='normal')
                    self.out.insert('end', msg + '\n')
                    self.out.see('end')
                    self.out.configure(state='disabled')
                elif kind == 'task':
                    task_id, manifest = evt[1], evt[2]
                    card = self._cards.get(task_id)
                    if card is not None:
                        card.apply_manifest(manifest)
                    self._completed_tasks += 1
                    if self._completed_tasks < self._total_tasks:
                        self.summary_lbl.configure(
                            text=self._header_text_progress())
                    self._refresh_apply_state()
                elif kind == 'dry_done':
                    total_rows, total_bytes = evt[1], evt[2]
                    self.summary_lbl.configure(
                        text=self._header_text_done(
                            total_rows, total_bytes))
                    self._log(f'--- dry-run complete: '
                              f'{total_rows:,} items / '
                              f'~{_human_bytes(total_bytes)} '
                              f'reclaimable across '
                              f'{self._total_tasks} categories ---')
                    self._busy = False
                    self.refresh_btn.configure(state='normal')
                    self._refresh_apply_state()
                elif kind == 'apply_done':
                    res = evt[1]
                    self._on_apply_done(res)
                elif kind == 'resume_verified':
                    # v4.14.6.30: deferred liveness-check result.
                    res = evt[1]
                    self._on_resume_verified(res)
        except queue.Empty:
            pass
        finally:
            try:
                self.win.after(200, self._drain_progress_q)
            except tk.TclError:
                pass

    # ─── Dry-run ────────────────────────────────────────────────────

    def _start_dry_run(self):
        if self._busy:
            return
        self._busy = True
        self._completed_tasks = 0
        self.refresh_btn.configure(state='disabled')
        self.apply_btn.configure(state='disabled')
        # Reset all cards to computing… and rebuild placeholder set in
        # case the registry has changed (it won't, but safe-by-design).
        self._render_placeholder_cards()
        self.summary_lbl.configure(text=self._header_text_initial())
        self._log('--- starting dry-run ---')
        threading.Thread(
            target=self._dry_run_worker, daemon=True).start()

    def _dry_run_worker(self):
        try:
            manifests = self.engine.dry_run_all(
                progress_callback=self._post_task_done)
        except Exception as e:
            self._log(f'dry-run error: {type(e).__name__}: {e}')
            manifests = []
        # Tally totals + signal overall done.
        total_rows = total_bytes = 0
        for m in manifests:
            total_rows += int(m.get('would_remove', 0) or 0)
            total_rows += int(m.get('would_archive', 0) or 0)
            total_bytes += int(m.get('bytes_freed', 0) or 0)
        try:
            self._progress_q.put(
                ('dry_done', total_rows, total_bytes))
        except Exception:
            pass

    def _refresh_apply_state(self):
        any_sel = any(
            c.var.get() for c in self._cards.values())
        ok = (any_sel and not self._busy)
        self.apply_btn.configure(
            state='normal' if ok else 'disabled')

    # ─── Excluded-ticker editor ─────────────────────────────────────

    def _edit_excluded(self):
        dlg = tk.Toplevel(self.win)
        dlg.title('Excluded tickers (gossip purge)')
        dlg.geometry('340x420')
        bg = self.colours.get('bg', '#0e1116')
        fg = self.colours.get('text', '#dcdfe6')
        muted = self.colours.get('muted', '#7a828e')
        dlg.configure(bg=bg)
        tk.Label(dlg, text='One ticker per line. The gossip purge '
                            'will SKIP these.',
                 bg=bg, fg=muted, font=('Segoe UI', 8),
                 wraplength=300, justify='left').pack(
            anchor='w', padx=10, pady=(10, 4))
        txt = scrolledtext.ScrolledText(
            dlg, height=20, bg='#181d23', fg=fg,
            font=('Consolas', 9), insertbackground=fg)
        txt.pack(fill='both', expand=True, padx=10, pady=4)
        if self._excluded_tickers:
            txt.insert('1.0', '\n'.join(sorted(self._excluded_tickers)))

        def _save():
            raw = txt.get('1.0', 'end').strip().splitlines()
            self._excluded_tickers = {
                line.strip().upper()
                for line in raw if line.strip()
            }
            self._log(f'Excluded tickers: '
                      f'{sorted(self._excluded_tickers) or "(none)"}')
            dlg.destroy()

        btns = tk.Frame(dlg, bg=bg)
        btns.pack(fill='x', padx=10, pady=8)
        tk.Button(btns, text='Save', bg=bg, fg=fg, relief='flat',
                   cursor='hand2', command=_save).pack(side='right')
        tk.Button(btns, text='Cancel', bg=bg, fg=fg, relief='flat',
                   cursor='hand2', command=dlg.destroy).pack(
            side='right', padx=(0, 6))

    # ─── Apply ──────────────────────────────────────────────────────

    def _on_apply(self):
        selected = [
            tid for tid, c in self._cards.items() if c.var.get()]
        if not selected:
            return
        sum_rows = sum(
            int((c.manifest or {}).get('would_remove', 0) or 0) +
            int((c.manifest or {}).get('would_archive', 0) or 0)
            for tid, c in self._cards.items() if tid in selected)
        sum_bytes = sum(
            int((c.manifest or {}).get('bytes_freed', 0) or 0)
            for tid, c in self._cards.items() if tid in selected)
        cats = ', '.join(
            self._cards[tid].label for tid in selected
            if tid in self._cards)
        msg = (
            f'Apply maintenance?\n\n'
            f'Categories: {cats}\n'
            f'Estimated removal/archive: {sum_rows:,} items\n'
            f'Estimated disk reclaim:     ~{_human_bytes(sum_bytes)}\n\n'
            f'The app will pause background tasks, clean, VACUUM the '
            f'affected DBs, then resume — no app restart required. A '
            f'manifest log will be written to data/.')
        if not messagebox.askyesno(
                'Confirm maintenance', msg, parent=self.win):
            return
        self._busy = True
        self.apply_btn.configure(state='disabled')
        self.refresh_btn.configure(state='disabled')
        self.excl_btn.configure(state='disabled')
        self.close_btn.configure(state='disabled')
        self._log('--- APPLY: starting ---')
        options_by_id = {
            'gossip_removal': {
                'excluded_tickers': tuple(self._excluded_tickers),
            },
        }
        threading.Thread(
            target=self._apply_worker,
            args=(selected, options_by_id),
            daemon=True).start()

    def _apply_worker(self, selected, options_by_id):
        # v4.14.6.30: callback fires AFTER the deferred liveness check
        # completes on the main thread. We just bridge it to the UI
        # queue so all UI updates stay on the main thread.
        def _on_verified(run):
            try:
                self._progress_q.put(('resume_verified', run))
            except Exception:
                pass

        try:
            res = self.engine.apply_selected(
                selected, options_by_id=options_by_id,
                on_resume_verified=_on_verified)
        except Exception as e:
            res = {
                'results': [],
                'log': [f'apply error: {type(e).__name__}: {e}'],
                'pause_log': [], 'resume_log': [],
                'resume_verified': {
                    '_status': {'state': 'skipped',
                                 'message': 'apply raised'}},
                'manifest_path': '',
            }
        try:
            self._progress_q.put(('apply_done', res))
        except Exception:
            pass

    def _on_apply_done(self, res: dict):
        # v4.14.6.30: cleanup is done, but the post-resume liveness
        # check is still scheduled (~2.5s out, possibly +1.5s for a
        # retry recheck). Save the partial result and show an interim
        # "verifying" state. The final completion dialog fires from
        # `_on_resume_verified` once the check completes — see below.
        self._apply_result = res
        total_removed = sum(
            (r.get('removed', 0) or 0) + (r.get('archived', 0) or 0)
            for r in res.get('results', []))
        total_bytes = sum(
            int(r.get('bytes_freed', 0) or 0)
            for r in res.get('results', []))
        errs = []
        for r in res.get('results', []):
            errs.extend(r.get('errors') or [])
        self._apply_totals = (total_removed, total_bytes, errs)
        self._log(f'--- APPLY complete: {total_removed:,} items / '
                  f'~{_human_bytes(total_bytes)} freed ---')
        if errs:
            self._log(f'  errors: {len(errs)}')
            for e in errs[:8]:
                self._log(f'    {e}')
        mp = res.get('manifest_path')
        if mp:
            self._log(f'manifest saved: {mp}')

        # Whether the engine is going to run a verification phase.
        rv = res.get('resume_verified') or {}
        rv_status = (rv.get('_status') or {}).get('state', 'pending')
        if rv_status == 'pending':
            self._log('verifying background tasks resumed…')
            # Keep the buttons disabled — the run isn't really done yet.
            # When verification completes, _on_resume_verified will
            # re-enable + show the final dialog.
        else:
            # No verification scheduled (no daemons paused) — finalize
            # right away.
            self._finalize_apply_dialog(res)

    def _on_resume_verified(self, res: dict):
        """v4.14.6.30 — fired (on main thread) when the deferred
        liveness check completes. Builds a summary of which daemons
        are alive / recovered / DEAD / unverified, surfaces dead ones
        prominently in the completion dialog, and finalizes UI state."""
        verified = (res.get('resume_verified') or {})
        # Strip the meta '_status' entry; the rest are real daemons.
        daemon_entries = {
            k: v for k, v in verified.items() if not k.startswith('_')
        }
        dead = [v.get('display', k) for k, v in daemon_entries.items()
                if v.get('state') == 'DEAD']
        recovered = [v.get('display', k)
                     for k, v in daemon_entries.items()
                     if v.get('state') == 'recovered_on_retry']
        unverified = [v.get('display', k)
                      for k, v in daemon_entries.items()
                      if v.get('state') == 'unverified']
        alive = [v.get('display', k)
                 for k, v in daemon_entries.items()
                 if v.get('state') == 'alive']

        if dead:
            self._log(
                f'⚠ background task(s) did NOT resume: '
                f'{", ".join(dead)}. Restart the app to restore them.')
        if recovered:
            self._log(
                f'background task(s) recovered on retry: '
                f'{", ".join(recovered)}.')
        if unverified:
            self._log(
                f'background task(s) state could not be confirmed '
                f'(module did not expose a thread handle): '
                f'{", ".join(unverified)}.')
        if alive and not dead:
            self._log(
                f'all background tasks resumed: {", ".join(alive)}.')

        self._finalize_apply_dialog(res, dead=dead, recovered=recovered,
                                    unverified=unverified, alive=alive)

    def _finalize_apply_dialog(self, res: dict,
                                dead=None, recovered=None,
                                unverified=None, alive=None):
        """Show the final completion dialog (replaces the original
        in-line messagebox at the end of _on_apply_done). Distinguishes
        ✅ all-resumed from ⚠ N-did-not-resume."""
        dead = dead or []
        recovered = recovered or []
        unverified = unverified or []
        alive = alive or []
        # Re-tally totals (may have changed if rare race; use the saved
        # totals when present).
        if getattr(self, '_apply_totals', None) is not None:
            total_removed, total_bytes, errs = self._apply_totals
        else:
            total_removed = sum(
                (r.get('removed', 0) or 0) + (r.get('archived', 0) or 0)
                for r in res.get('results', []))
            total_bytes = sum(
                int(r.get('bytes_freed', 0) or 0)
                for r in res.get('results', []))
            errs = []
            for r in res.get('results', []):
                errs.extend(r.get('errors') or [])
        mp = res.get('manifest_path')

        # Build the dialog text.
        lines = [
            f'{total_removed:,} items removed/archived.',
            f'~{_human_bytes(total_bytes)} freed.',
            '',
        ]
        if dead:
            lines.append(
                f'⚠ Background task(s) did NOT resume: '
                f'{", ".join(dead)}.')
            lines.append(
                '  Restart the app to restore them.')
        elif (alive or recovered) and not unverified:
            lines.append('✅ All background tasks resumed.')
        elif alive or recovered or unverified:
            # Partial-confidence state: some alive, some unverified
            # (no DEAD) — call it out honestly without alarm.
            parts = []
            if alive: parts.append(f'{len(alive)} alive')
            if recovered: parts.append(f'{len(recovered)} recovered')
            if unverified: parts.append(f'{len(unverified)} unverified')
            lines.append(
                f'Background tasks: {", ".join(parts)}.')
            if unverified:
                lines.append(
                    '  (Unverified = module did not expose a thread '
                    'handle; not a failure.)')
        if recovered and not dead:
            lines.append('')
            lines.append(
                f'Recovered on retry: {", ".join(recovered)}.')
        if errs:
            lines.append('')
            lines.append(f'Errors: {len(errs)} (see log)')
        if mp:
            lines.append('')
            lines.append(f'Manifest: {mp}')

        self._busy = False
        self.refresh_btn.configure(state='normal')
        self.excl_btn.configure(state='normal')
        self.close_btn.configure(state='normal')
        title = (
            'Maintenance complete — ⚠ tasks did not resume'
            if dead else 'Maintenance complete')
        # Use showwarning when something didn't come back so the
        # platform icon matches the severity; showinfo otherwise.
        if dead:
            messagebox.showwarning(title, '\n'.join(lines),
                                    parent=self.win)
        else:
            messagebox.showinfo(title, '\n'.join(lines),
                                 parent=self.win)


def open_maintenance_window(root: tk.Misc, app) -> MaintenanceWindow:
    """Convenience entry point — call from the menu hook."""
    return MaintenanceWindow(root, app)
