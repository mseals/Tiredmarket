"""tm_reasoning_view — shared consensus-reasoning Toplevel.

v4.14.5.93-recommend-reasoning (2026-06-10): factored out from
tm_portfolio_panel._open_reasoning_window so both Portfolio's
"View reasoning" button and Recommend's "Why?" button render the
same window from the same code. Behaviour unchanged for Portfolio
(its wrapper passes the same arguments it always did, with
source_label=None preserving the existing header).

Public API (module-level):
    open_reasoning_window(parent, ticker, consensus, *,
                          c, space, fonts,
                          source_label=None,
                          humanize_age=None)

Renders a 680x520 Toplevel showing each model's full response text
from `consensus['votes']`. Per-model rows are: <model name> <DIRECTION>
on a header line, followed by the full `response` body, separated
by ─ rules. `source_label` (when given) renders as a one-line italic
header beneath the ticker/age line so the user always knows whether
they're reading a light background pass (Layer-2 3-model) or a deep
live check (Verify 7-model). `humanize_age` is the project's
existing _humanize_age helper; pass it from the caller so this
module stays import-free of any panel/app surface.

NO new AI calls. NO storage changes. Pure display.
"""

from __future__ import annotations

import tkinter as tk


def open_reasoning_window(parent, ticker, consensus, *,
                            c, space, fonts,
                            source_label: str | None = None,
                            humanize_age=None):
    """Open the consensus-reasoning Toplevel.

    Args:
        parent: a tk widget used as the Toplevel's master + the
            centering reference (its toplevel is read for geometry).
        ticker: pick ticker symbol, displayed in the header.
        consensus: dict with at minimum {'ts': str, 'votes': list},
            where each vote is {'model', 'direction', 'response', ...}.
        c: theme colour dict (same shape as panel.c / app.c).
        space: spacing dict (same shape as panel.space / app.space).
        fonts: font dict (same shape as panel.fonts / app.fonts).
        source_label: optional one-line string to render under the
            header — e.g. "Source: 7-model Verify · 5:44pm" or
            "Source: 3-model background check (click Verify for a
            wider live check)". None preserves the legacy header
            shape (Portfolio's pre-refactor behaviour).
        humanize_age: callable(ts_str) → relative age string; the
            project's existing _humanize_age helper. None falls back
            to the raw ts string.

    Behaviour mirrors the original tm_portfolio_panel
    `_open_reasoning_window` byte-for-byte when called with
    source_label=None — Portfolio's "View reasoning" sees no change.
    """
    win = tk.Toplevel(parent)
    win.title(f"{ticker} — consensus reasoning")
    win.configure(bg=c['bg'])
    try:
        parent_root = parent.winfo_toplevel()
        parent_root.update_idletasks()
        px = parent_root.winfo_rootx()
        py = parent_root.winfo_rooty()
        pw = parent_root.winfo_width()
        ph = parent_root.winfo_height()
        dw, dh = 680, 520
        dx = px + (pw - dw) // 2
        dy = py + (ph - dh) // 2
        win.geometry(f"{dw}x{dh}+{dx}+{dy}")
    except Exception:
        win.geometry("680x520")

    hdr = tk.Frame(win, bg=c['card'], padx=space['md'], pady=space['sm'])
    hdr.pack(side='top', fill='x')
    if humanize_age is not None:
        try:
            _age = humanize_age(consensus.get('ts'))
        except Exception:
            _age = ''
    else:
        _age = str(consensus.get('ts') or '')
    tk.Label(
        hdr, text=f"{ticker}  ·  {_age}",
        bg=c['card'], fg=c['accent'],
        font=fonts['heading']
    ).pack(side='left')
    tk.Button(
        hdr, text='Close', bg=c['card2'], fg=c['text'],
        relief='flat', borderwidth=0, cursor='hand2',
        padx=12, pady=4, font=fonts['body_bold'],
        command=win.destroy
    ).pack(side='right')

    # v4.14.5.93-recommend-reasoning: honest-source one-liner. Only
    # rendered when the caller supplied a label, so Portfolio (which
    # passes None) gets the exact pre-refactor look.
    if source_label:
        src = tk.Frame(win, bg=c['bg'], padx=space['md'],
                       pady=space.get('xs', 4))
        src.pack(side='top', fill='x')
        tk.Label(
            src, text=str(source_label),
            bg=c['bg'], fg=c.get('dim', '#888'),
            font=fonts.get('body', ('Segoe UI', 9, 'italic')),
            anchor='w', justify='left'
        ).pack(side='left', fill='x', expand=True)

    body = tk.Frame(win, bg=c['bg'])
    body.pack(fill='both', expand=True)

    text = tk.Text(body, bg=c['bg'], fg=c['text'],
                   insertbackground=c['accent'], relief='flat',
                   wrap='word', padx=space['md'], pady=space['md'],
                   font=fonts['body'])
    sb = tk.Scrollbar(body, orient='vertical', command=text.yview)
    text.configure(yscrollcommand=sb.set)
    text.pack(side='left', fill='both', expand=True)
    sb.pack(side='right', fill='y')

    text.tag_configure('model', foreground=c['accent'],
                       font=fonts['body_bold'])
    text.tag_configure('verdict', foreground=c['amber'],
                       font=fonts['body_bold'])
    text.tag_configure('sep', foreground=c['dim'])

    # Canonicalize model display names (stage-6d), with a safe no-op
    # fallback so this module imports headlessly in tests / audits.
    try:
        import tm_api_providers as _tmap
        _norm = _tmap.canonicalize_model_label
    except Exception:
        _norm = lambda x: x

    votes = consensus.get('votes', []) or []
    if not votes:
        text.insert('end', "(No model responses recorded.)\n")
    for v in votes:
        text.insert('end', _norm(v.get('model', '?')) + '  ', 'model')
        text.insert(
            'end', (v.get('direction', '?') or '?').upper(), 'verdict')
        text.insert('end', '\n')
        response = v.get('response', '') or '(no response saved)'
        text.insert('end', response.strip() + '\n\n')
        text.insert('end', '─' * 60 + '\n\n', 'sep')

    text.config(state='disabled')
    return win
