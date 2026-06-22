"""tm_teacher_splash — Teacher AI MVP Session 4: startup splash + disclaimer.

The splash appears immediately when the app launches, replacing the legacy
center-screen loading overlay. It owns disclaimer delivery on first launch
(via transition_splash_to_disclaimer) and closes silently for returning
users (via close_splash).

Public API:
    show_splash(root) -> splash_handle
    transition_splash_to_disclaimer(splash, disclaimer_text, on_acknowledge)
    close_splash(splash) -> None

Visual language matches tm_teacher_ai's canvas surfaces — accent left-edge
stripe, accent border on the card, "Tired Market AI" header. The splash is
the first thing the user sees AI doing, so it sets the visual idiom for
everything that follows.

Per design/teacher_ai_mvp.md "Splash window at startup".
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional


# Animation timing
_PULSE_MS = 350           # Loading-dots tick cadence
_FADE_STEPS = 8
_FADE_STEP_MS = 18

# Theme fallback (matches tm_teacher_ai's _FALLBACK_C — used only if the
# root doesn't carry a `_teacher_ai_app` reference).
_FALLBACK_C = {
    'bg': '#0e1116', 'card': '#161b22', 'card2': '#1c232c',
    'border': '#30363d', 'fg': '#e6edf3', 'muted': '#9aa4af',
    'dim': '#6e7681', 'accent': '#58a6ff',
}

_FALLBACK_FONTS = {
    'body': ('Segoe UI', 10),
    'body_bold': ('Segoe UI', 10, 'bold'),
    'caption': ('Segoe UI', 8),
    'h2': ('Segoe UI', 14, 'bold'),
}


def _theme(root) -> dict:
    app = getattr(root, '_tm_app', None) or _app_via_canvas(root)
    if app is not None and hasattr(app, 'c'):
        return app.c
    return _FALLBACK_C


def _fonts(root) -> dict:
    app = getattr(root, '_tm_app', None) or _app_via_canvas(root)
    if app is not None and hasattr(app, 'fonts'):
        return app.fonts
    return _FALLBACK_FONTS


def _app_via_canvas(root):
    """Best-effort App lookup. The canvas has a `_teacher_ai_app` attribute
    (set in tired_market.py for tm_teacher_ai). If we can find it on any
    child widget of root, return that App."""
    try:
        for w in root.winfo_children():
            app = getattr(w, '_teacher_ai_app', None)
            if app is not None:
                return app
    except Exception:
        pass
    return None


# ─── Public API ───────────────────────────────────────────────────────

def show_splash(root) -> dict:
    """Create and show the splash window. Returns an opaque handle dict
    callers pass to transition_* and close_splash."""
    c = _theme(root)
    fnts = _fonts(root)

    accent = c.get('accent', _FALLBACK_C['accent'])
    card = c.get('card', _FALLBACK_C['card'])
    bg = c.get('bg', _FALLBACK_C['bg'])
    fg = c.get('fg', _FALLBACK_C['fg'])
    muted = c.get('muted', _FALLBACK_C['muted'])

    title_font = fnts.get('h2', _FALLBACK_FONTS['h2'])
    body_font = fnts.get('body', _FALLBACK_FONTS['body'])
    bold_font = fnts.get('body_bold', _FALLBACK_FONTS['body_bold'])

    win = tk.Toplevel(root)
    win.title("Tired Market AI")
    win.configure(bg=accent)  # outer bg becomes the accent stripe
    # Intentionally NOT transient(root): when root is withdrawn during
    # the disclaimer flow, a transient Toplevel won't map on Windows
    # (Win32 hides transients whose master is withdrawn). Keeping the
    # splash as a regular Toplevel guarantees it displays regardless of
    # root's visibility state. Tradeoff: a brief separate taskbar entry
    # during the splash phase, which fits the "AI as front door"
    # product framing anyway.
    try:
        # Block the main window until splash dismisses (or transitions to
        # disclaimer, after which the user must acknowledge to proceed).
        win.grab_set()
    except Exception:
        pass

    # X-button → quit (matches the legacy disclaimer modal's behavior).
    try:
        win.protocol("WM_DELETE_WINDOW", lambda: root.quit())
    except Exception:
        pass

    # Geometry: 480x280, centered on screen.
    width, height = 480, 280
    try:
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = max(0, (sw - width) // 2)
        y = max(0, (sh - height) // 3)  # slightly above center reads better
        win.geometry(f"{width}x{height}+{x}+{y}")
    except Exception:
        win.geometry(f"{width}x{height}")
    win.resizable(False, False)

    # Inner card. 5px accent stripe shows on the left via the win's bg
    # bleeding through the inner's pack offset.
    inner = tk.Frame(win, bg=card,
                     highlightthickness=1,
                     highlightbackground=accent, bd=0)
    inner.pack(side='right', fill='both', expand=True, padx=(5, 0))

    pad = tk.Frame(inner, bg=card)
    pad.pack(fill='both', expand=True, padx=24, pady=20)

    # Header: identifies the speaker.
    tk.Label(pad, text="Tired Market AI",
             bg=card, fg=accent, font=bold_font, anchor='w'
             ).pack(side='top', fill='x', pady=(0, 12))

    # Body label — swaps content on transition.
    body_lbl = tk.Label(pad, text="Loading Tired Market AI",
                        bg=card, fg=fg, font=title_font,
                        wraplength=420, justify='left', anchor='w')
    body_lbl.pack(side='top', fill='x', pady=(0, 8))

    # Subtitle / sub-line — explains briefly during loading; replaced on
    # transition by the disclaimer body text.
    sub_lbl = tk.Label(
        pad,
        text=("Getting your portfolio, predictions, and data sources ready. "
              "This usually takes a few seconds."),
        bg=card, fg=muted, font=body_font,
        wraplength=420, justify='left', anchor='w')
    sub_lbl.pack(side='top', fill='x', pady=(0, 14))

    # Button row — hidden during loading, populated on transition.
    btn_row = tk.Frame(pad, bg=card)
    btn_row.pack(side='bottom', fill='x')

    handle = {
        'win': win, 'root': root,
        'card': card, 'accent': accent, 'bg': bg, 'fg': fg, 'muted': muted,
        'body_font': body_font, 'bold_font': bold_font, 'title_font': title_font,
        'body_lbl': body_lbl, 'sub_lbl': sub_lbl, 'btn_row': btn_row,
        'pad': pad,
        'anim_id': None, 'anim_step': 0,
        'state': 'loading',
        'closed': False,
    }

    _start_pulse(handle)

    # Force initial mapping + lift so the splash reliably appears on top of
    # root. Important when show_splash is scheduled via root.after(0) — the
    # Toplevel may be created in an unmapped state on some Tk builds.
    try:
        win.update_idletasks()
        win.lift()
        win.focus_force()
    except Exception:
        pass

    return handle


def transition_splash_to_disclaimer(splash, disclaimer_text, on_acknowledge):
    """Swap the splash's loading content for the disclaimer + acknowledgment
    button. on_acknowledge() fires when the user clicks 'I understand'."""
    if splash is None or splash.get('closed'):
        return
    _stop_pulse(splash)
    splash['state'] = 'disclaimer'

    card = splash['card']
    fg = splash['fg']
    muted = splash['muted']
    accent = splash['accent']
    bg = splash['bg']

    body_lbl = splash['body_lbl']
    sub_lbl = splash['sub_lbl']

    # Resize the window to accommodate the longer disclaimer text + button.
    try:
        win = splash['win']
        width, height = 540, 360
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = max(0, (sw - width) // 2)
        y = max(0, (sh - height) // 3)
        win.geometry(f"{width}x{height}+{x}+{y}")
    except Exception:
        pass

    # Title becomes "Before we get started" — short, conversational.
    try:
        body_lbl.config(text="Before we get started")
    except Exception:
        pass

    # Wider wrap for the disclaimer body.
    try:
        sub_lbl.config(text=disclaimer_text, fg=fg,
                       font=splash['body_font'], wraplength=480)
    except Exception:
        pass

    # Single "I understand" button — primary action style.
    btn_row = splash['btn_row']
    for child in list(btn_row.winfo_children()):
        try:
            child.destroy()
        except Exception:
            pass

    def _on_click():
        try:
            if callable(on_acknowledge):
                on_acknowledge()
        except Exception:
            pass

    def _on_exit():
        # User declines the disclaimer — quit the app cleanly. The legacy
        # marker file is not written, so next launch re-prompts.
        try:
            splash['win'].master.quit()
        except Exception:
            pass

    # Pack order: 'I understand' first with side='right' so it ends up
    # rightmost; 'Exit' next with side='right' so it lands to the left
    # of 'I understand'. Primary action right, secondary action left.
    card2 = splash.get('card', '#161b22')
    muted = splash['muted']
    tk.Button(btn_row, text="I understand",
              bg=accent, fg=bg,
              relief='flat', padx=18, pady=6,
              font=splash['bold_font'], cursor='hand2', borderwidth=0,
              activebackground=accent, activeforeground=bg,
              command=_on_click
              ).pack(side='right', pady=(8, 0))
    tk.Button(btn_row, text="Exit",
              bg=card2, fg=muted,
              relief='flat', padx=14, pady=6,
              font=splash['body_font'], cursor='hand2', borderwidth=0,
              activebackground=card2, activeforeground=muted,
              command=_on_exit
              ).pack(side='right', padx=(0, 8), pady=(8, 0))

    # Force a paint pass so the swapped-in disclaimer body + button are
    # visible immediately. Without this, the transition may sit invisible
    # for one event-loop tick.
    try:
        splash['win'].update_idletasks()
        splash['win'].lift()
        splash['win'].focus_force()
    except Exception:
        pass


def close_splash(splash) -> None:
    """Animate the splash out (alpha fade if supported) and destroy it.
    Idempotent — second call is a no-op."""
    if splash is None or splash.get('closed'):
        return
    splash['closed'] = True
    _stop_pulse(splash)

    win = splash.get('win')
    if win is None:
        return

    try:
        win.grab_release()
    except Exception:
        pass

    # Try alpha fade-out. Windows + macOS support -alpha; on Linux it
    # depends on the WM. If anything fails, fall through to instant destroy.
    try:
        win.attributes('-alpha', 1.0)
        _fade_out(win, step=0)
    except Exception:
        try:
            win.destroy()
        except Exception:
            pass


# ─── Internals ────────────────────────────────────────────────────────

def _start_pulse(splash) -> None:
    """Animate a growing ellipsis on the loading title."""
    win = splash.get('win')
    if win is None:
        return

    def tick():
        if splash.get('closed') or splash.get('state') != 'loading':
            splash['anim_id'] = None
            return
        try:
            step = splash.get('anim_step', 0)
            dots = "." * (step % 4)
            splash['body_lbl'].config(text=f"Loading Tired Market AI{dots}")
            splash['anim_step'] = step + 1
        except Exception:
            pass
        try:
            splash['anim_id'] = win.after(_PULSE_MS, tick)
        except Exception:
            splash['anim_id'] = None

    try:
        splash['anim_id'] = win.after(_PULSE_MS, tick)
    except Exception:
        splash['anim_id'] = None


def _stop_pulse(splash) -> None:
    aid = splash.get('anim_id')
    if aid is None:
        return
    try:
        splash['win'].after_cancel(aid)
    except Exception:
        pass
    splash['anim_id'] = None


def _fade_out(win, step: int) -> None:
    """Tick alpha down. Destroys the window when alpha reaches ~0."""
    if step >= _FADE_STEPS:
        try:
            win.destroy()
        except Exception:
            pass
        return
    try:
        alpha = max(0.0, 1.0 - (step + 1) / _FADE_STEPS)
        win.attributes('-alpha', alpha)
        win.after(_FADE_STEP_MS, lambda: _fade_out(win, step + 1))
    except Exception:
        try:
            win.destroy()
        except Exception:
            pass
