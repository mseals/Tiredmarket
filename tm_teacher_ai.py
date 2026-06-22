"""
tm_teacher_ai — Teacher AI MVP Session 2: visual surface rendering primitives.

Public API:
    show_center(canvas, message, actions=None, on_dismiss=None) -> handle
    show_near(canvas, target_widget, message, actions=None, on_dismiss=None) -> handle
    dismiss_surface(handle) -> None
    is_surface_active(canvas) -> bool

This module owns Teacher AI's visual language: animated message surfaces with
a distinctive accent-colored left edge marker and slide-in / slide-out motion.
It contains NO business logic — no prereq checks, no FAQ matching, no intercept
wiring. Sessions 3-5 add those layers on top.

Theme + fonts are read from the App if the canvas has an attached App reference
at `canvas._teacher_ai_app`. Otherwise a hand-tuned dark-theme fallback is used
so the module also renders in isolated test harnesses.

Per design/teacher_ai_mvp.md "AI surfaces on canvas".
"""
from __future__ import annotations

import tkinter as tk


# ─── Module state ─────────────────────────────────────────────────────
# Only one Teacher AI surface visible per canvas at a time. Keyed by id(canvas).
_active: dict = {}

# Persistent app-wide click handler for click-outside dismissal. Installed
# lazily on first surface, never removed for the life of the app. See
# _ensure_global_click_handler for the why.
_global_click_installed: bool = False


# ─── Animation timing ─────────────────────────────────────────────────
_FRAME_MS = 16          # ~60 FPS target; weak hardware degrades gracefully.
_IN_MS = 220            # Slide-in duration (sits inside the 200-300ms band).
_OUT_MS = 160           # Slide-out duration (150-200ms band).

# Slide distance for show_near surfaces (px). Center surfaces slide from off-canvas.
_NEAR_SLIDE_PX = 30


# ─── Theme fallback ───────────────────────────────────────────────────
# Hand-tuned to roughly match the Tired Market dark theme. Only used when
# canvas._teacher_ai_app is not set (e.g., unit tests, dev harnesses).
_FALLBACK_C: dict = {
    'bg': '#0e1116', 'card': '#161b22', 'card2': '#1c232c',
    'border': '#30363d', 'fg': '#e6edf3', 'muted': '#9aa4af',
    'dim': '#6e7681', 'accent': '#58a6ff',
    'green': '#3fb950', 'red': '#f85149', 'amber': '#d29922',
}

_FALLBACK_FONTS: dict = {
    'body': ('Segoe UI', 10),
    'body_bold': ('Segoe UI', 10, 'bold'),
    'caption': ('Segoe UI', 8),
    'h2': ('Segoe UI', 12, 'bold'),
}


def _theme(canvas) -> dict:
    """Return the active App's color dict, or the fallback."""
    app = getattr(canvas, '_teacher_ai_app', None)
    if app is not None and hasattr(app, 'c'):
        return app.c
    return _FALLBACK_C


def _fonts(canvas) -> dict:
    """Return the active App's font dict, or the fallback."""
    app = getattr(canvas, '_teacher_ai_app', None)
    if app is not None and hasattr(app, 'fonts'):
        return app.fonts
    return _FALLBACK_FONTS


# ─── Public API ───────────────────────────────────────────────────────

def _mark_just_created(handle) -> None:
    """Flag a freshly-built surface so the global click handler ignores
    clicks for the next 150ms.

    Why: the click that triggers a summon (e.g., on the corner indicator
    or a future toolbar button) propagates through bind_all AFTER the
    summon callback creates the surface. The global click-outside handler
    would then immediately dismiss the brand-new surface because the
    click landed outside its bbox. This grace window covers any future
    trigger that summons a surface via the same click event.
    """
    handle['_just_created'] = True
    canvas = handle.get('canvas')
    if canvas is None:
        return

    def _clear():
        try:
            handle['_just_created'] = False
        except Exception:
            pass

    try:
        canvas.after(150, _clear)
    except Exception:
        pass


def _cancel_after_ids(handle) -> None:
    """Cancel pending Tk after() callbacks tied to a handle.

    Used at supersession points (a new surface replacing an old one) and
    at destroy time. Without this, a stale animation tick can fire after
    Tk has reused the previous surface's canvas item ID for the new
    surface, dragging the new surface around on its first frames.
    """
    if not handle:
        return
    canvas = handle.get('canvas')
    if canvas is None:
        return
    for _key in ('_anim_after_id', '_focus_after_id'):
        aid = handle.get(_key)
        if aid is not None:
            try:
                canvas.after_cancel(aid)
            except Exception:
                pass
            handle[_key] = None
    # v4.14.5.54-recs-gate-budgetaware: cancel any queued `then=build` chained
    # off this handle's animate-out. When a second surface tries to replace one
    # that's ALREADY dismissing, _animate_out queues the new build to run after
    # the dismiss completes; if the user is force-dismissing, that queued build
    # would RESURRECT a surface right after Dismiss (the "button did nothing"
    # bug). Cancelling them here makes a user dismiss final.
    chained = handle.get('_chained_after_ids')
    if chained:
        for aid in list(chained):
            try:
                canvas.after_cancel(aid)
            except Exception:
                pass
        handle['_chained_after_ids'] = []


def is_surface_active(canvas) -> bool:
    """True if a Teacher AI surface is currently visible on this canvas."""
    return id(canvas) in _active


def dismiss_surface(handle) -> None:
    """Force-dismiss a previously-shown surface. Animates out and cleans up."""
    if handle is None:
        return
    _animate_out(handle)


def dismiss_active(canvas) -> None:
    """v4.14.5.51: dismiss whatever surface is currently active on `canvas`.
    Used by the 'dismiss' action ('Not now') so it explicitly removes the
    surface instead of relying on the generic post-click animate-out (which
    broke when another surface had replaced the one on the canvas). Never
    raises; no-op when nothing is active."""
    try:
        h = _active.get(id(canvas))
        if h is not None:
            _animate_out(h)
    except Exception:
        pass


def show_center(canvas, message, actions=None, on_dismiss=None):
    """Surface a centered Teacher AI message. Slides in from above, slides out
    when dismissed. Returns a handle for dismiss_surface()."""
    existing = _active.get(id(canvas))
    if existing is not None:
        # Replace: animate the old one out, then build the new one.
        # Cancel the old surface's pending in-animation tick eagerly so it
        # can't fire and disturb the replacement build.
        _cancel_after_ids(existing)
        _animate_out(existing, then=lambda: _build_center(
            canvas, message, actions, on_dismiss))
        return None
    return _build_center(canvas, message, actions, on_dismiss)


def show_input(canvas, prompt, on_submit, on_cancel=None, disclaimer=None):
    """Surface a Teacher AI message with an inline single-line Entry.

    See module docstring for argument semantics. The Entry takes keyboard
    focus automatically; Enter inside the Entry triggers Submit; Submit
    reads the text, animates the surface out, then calls on_submit(text).
    Cancel animates out and calls on_cancel() if provided.

    `disclaimer` (slice 10): an optional small, persistent muted line shown
    BELOW the entry on the ask screen (before the user submits). The Ask AI
    front door uses it to establish the not-a-financial-advisor framing up
    front, so the framing no longer rides on every answer.

    Session 5: used by the Ctrl+` summon flow to collect a free-form
    question from the user, which is then keyword-matched against
    data/internal/faq.json.
    """
    existing = _active.get(id(canvas))
    if existing is not None:
        _cancel_after_ids(existing)
        _animate_out(existing, then=lambda: _build_input(
            canvas, prompt, on_submit, on_cancel, disclaimer))
        return None
    return _build_input(canvas, prompt, on_submit, on_cancel, disclaimer)


def show_near(canvas, target_widget, message, actions=None, on_dismiss=None):
    """Surface a Teacher AI message near a specific widget (e.g., a toolbar
    button). Positions adjacent to the target with a small arrow pointing at
    it. Returns a handle for dismiss_surface()."""
    existing = _active.get(id(canvas))
    if existing is not None:
        _cancel_after_ids(existing)
        _animate_out(existing, then=lambda: _build_near(
            canvas, target_widget, message, actions, on_dismiss))
        return None
    return _build_near(canvas, target_widget, message, actions, on_dismiss)


# ─── Frame construction ───────────────────────────────────────────────

def _build_message_frame(canvas, message, actions, max_width):
    """Construct the visual frame (outer accent stripe + inner card + content)
    for a Teacher AI surface. Returns (outer_frame, action_callbacks_done).

    The action callbacks are wired to call the user's callback (if any) and
    then animate the surface out. on_dismiss firing is handled inside the
    out-animation chain so it always fires once per surface.
    """
    c = _theme(canvas)
    fnts = _fonts(canvas)

    accent = c.get('accent', _FALLBACK_C['accent'])
    card = c.get('card', _FALLBACK_C['card'])
    card2 = c.get('card2', _FALLBACK_C['card2'])
    fg = c.get('fg', _FALLBACK_C['fg'])
    bg = c.get('bg', _FALLBACK_C['bg'])

    body_font = fnts.get('body', _FALLBACK_FONTS['body'])
    bold_font = fnts.get('body_bold', _FALLBACK_FONTS['body_bold'])

    # outer = the 5px accent stripe on the left. The right side packs the inner
    # card which fills the rest. Together they form the surface.
    outer = tk.Frame(canvas, bg=accent, highlightthickness=0, bd=0)

    inner = tk.Frame(outer, bg=card,
                     highlightthickness=1, highlightbackground=accent, bd=0)
    inner.pack(side='right', fill='both', expand=True, padx=(5, 0))

    pad = tk.Frame(inner, bg=card)
    pad.pack(fill='both', expand=True, padx=18, pady=16)

    # Header line — identifies the speaker.
    tk.Label(pad, text="Tired Market AI",
             bg=card, fg=accent, font=bold_font, anchor='w'
             ).pack(side='top', fill='x', pady=(0, 8))

    # Body message. wraplength constrains width-driven wrapping.
    wrap = max(max_width - 60, 200)
    tk.Label(pad, text=message,
             bg=card, fg=fg, font=body_font,
             wraplength=wrap, justify='left', anchor='w'
             ).pack(side='top', fill='x', pady=(0, 14))

    # Default action: a single "Dismiss" if caller didn't supply any.
    if not actions:
        actions = [{"label": "Dismiss", "callback": None}]

    # Action row. First entry is visually prominent (accent bg), rest muted.
    btn_row = tk.Frame(pad, bg=card)
    btn_row.pack(side='top', fill='x')

    action_buttons = []
    for i, act in enumerate(actions):
        label = act.get('label', 'OK')
        cb = act.get('callback')
        if i == 0:
            b_bg, b_fg = accent, bg
        else:
            b_bg, b_fg = card2, fg
        b = tk.Button(btn_row, text=label, bg=b_bg, fg=b_fg,
                      relief='flat', padx=12, pady=4,
                      font=bold_font, cursor='hand2', borderwidth=0,
                      activebackground=b_bg, activeforeground=b_fg,
                      command=lambda _cb=cb: _on_action_click(canvas, _cb))
        b.pack(side='right', padx=(6, 0))
        action_buttons.append(b)

    return outer


def _on_action_click(canvas, user_cb):
    """User clicked an action button. Run their callback, then dismiss."""
    try:
        if callable(user_cb):
            user_cb()
    except Exception:
        pass
    h = _active.get(id(canvas))
    if h is not None:
        _animate_out(h)


# ─── Surface builders ─────────────────────────────────────────────────

def _build_center(canvas, message, actions, on_dismiss):
    canvas.update_idletasks()
    cw = max(canvas.winfo_width(), 200)
    ch = max(canvas.winfo_height(), 200)

    width = min(560, max(280, cw - 40))
    frame = _build_message_frame(canvas, message, actions, width)
    frame.update_idletasks()
    height = max(frame.winfo_reqheight(), 100)
    # If we somehow over-shot the canvas, clamp.
    height = min(height, ch - 40)

    final_x = cw // 2
    final_y = ch // 2
    start_y = -(height // 2 + 20)

    item = canvas.create_window(final_x, start_y, anchor='center',
                                window=frame, width=width, height=height)

    handle = {
        'canvas': canvas, 'item': item, 'frame': frame,
        'final_x': final_x, 'final_y': final_y,
        'start_y': start_y,
        'width': width, 'height': height,
        'on_dismiss': on_dismiss, 'kind': 'center',
        'arrow_item': None,
        'click_funcid': None, 'esc_funcid': None,
        'dismissing': False,
    }
    _active[id(canvas)] = handle
    _mark_just_created(handle)

    _animate_in(handle)
    _wire_dismissal(handle)
    return handle


def _build_input(canvas, prompt, on_submit, on_cancel, disclaimer=None):
    """Build a centered surface with prompt text + Entry + Submit/Cancel.

    Mirrors _build_center's layout, but the action row is replaced with an
    Entry-then-buttons stack so the user can type a question. Submit reads
    the Entry value, dismisses the surface, then defers on_submit(text) to
    the next event loop tick (so the slide-out animation isn't blocked by
    whatever the caller does next, like building another surface).
    """
    canvas.update_idletasks()
    cw = max(canvas.winfo_width(), 200)
    ch = max(canvas.winfo_height(), 200)

    width = min(560, max(320, cw - 40))

    c = _theme(canvas)
    fnts = _fonts(canvas)

    accent = c.get('accent', _FALLBACK_C['accent'])
    card = c.get('card', _FALLBACK_C['card'])
    card2 = c.get('card2', _FALLBACK_C['card2'])
    fg = c.get('fg', _FALLBACK_C['fg'])
    bg = c.get('bg', _FALLBACK_C['bg'])

    body_font = fnts.get('body', _FALLBACK_FONTS['body'])
    bold_font = fnts.get('body_bold', _FALLBACK_FONTS['body_bold'])

    outer = tk.Frame(canvas, bg=accent, highlightthickness=0, bd=0)
    inner = tk.Frame(outer, bg=card,
                     highlightthickness=1, highlightbackground=accent, bd=0)
    inner.pack(side='right', fill='both', expand=True, padx=(5, 0))

    pad = tk.Frame(inner, bg=card)
    pad.pack(fill='both', expand=True, padx=18, pady=16)

    tk.Label(pad, text="Tired Market AI",
             bg=card, fg=accent, font=bold_font, anchor='w'
             ).pack(side='top', fill='x', pady=(0, 8))

    tk.Label(pad, text=prompt,
             bg=card, fg=fg, font=body_font,
             wraplength=max(width - 60, 200), justify='left', anchor='w'
             ).pack(side='top', fill='x', pady=(0, 10))

    # takefocus=0 prevents Tk's implicit focus-traversal from auto-grabbing
    # this Entry the moment it's mapped onto the canvas. Without it, on the
    # second-and-later summon (when the previously-focused Dismiss button
    # has been destroyed), Tk hands focus to the new Entry during mapping,
    # which fires auto-scroll-to-focus and fights the slide-in animation.
    # Flipped back to 1 by the deferred focus block below after the in-
    # animation completes.
    entry = tk.Entry(pad, bg=card2, fg=fg, font=body_font,
                     insertbackground=fg, relief='flat',
                     highlightthickness=1, highlightbackground=accent,
                     highlightcolor=accent, bd=0,
                     takefocus=0)
    entry.pack(side='top', fill='x', ipady=6, pady=(0, 12))

    # slice 10: persistent muted disclaimer line on the ASK screen (shown
    # before submit). Establishes the not-a-financial-advisor framing up
    # front so factual answers can stand clean (no per-answer footer).
    _disc = (disclaimer or '').strip()
    if _disc:
        muted = c.get('muted', _FALLBACK_C['muted'])
        caption_font = fnts.get('caption', _FALLBACK_FONTS['caption'])
        tk.Label(pad, text=_disc,
                 bg=card, fg=muted, font=caption_font,
                 wraplength=max(width - 60, 200), justify='left', anchor='w'
                 ).pack(side='top', fill='x', pady=(0, 10))

    btn_row = tk.Frame(pad, bg=card)
    btn_row.pack(side='top', fill='x')

    submitted = {'fired': False}

    def _do_submit(_e=None):
        if submitted['fired']:
            return
        submitted['fired'] = True
        try:
            text = entry.get()
        except Exception:
            text = ''
        h = _active.get(id(canvas))
        # Defer the caller's on_submit so the slide-out animation can
        # start cleanly before any new surface gets built on top.
        def _later():
            try:
                if callable(on_submit):
                    on_submit(text)
            except Exception:
                pass
        if h is not None:
            _animate_out(h, then=_later)
        else:
            _later()

    def _do_cancel():
        if submitted['fired']:
            return
        submitted['fired'] = True
        h = _active.get(id(canvas))
        def _later():
            try:
                if callable(on_cancel):
                    on_cancel()
            except Exception:
                pass
        if h is not None:
            _animate_out(h, then=_later)
        else:
            _later()

    submit_btn = tk.Button(btn_row, text="Ask", bg=accent, fg=bg,
                           relief='flat', padx=14, pady=4,
                           font=bold_font, cursor='hand2', borderwidth=0,
                           activebackground=accent, activeforeground=bg,
                           command=_do_submit)
    submit_btn.pack(side='right', padx=(6, 0))

    cancel_btn = tk.Button(btn_row, text="Cancel", bg=card2, fg=fg,
                           relief='flat', padx=12, pady=4,
                           font=bold_font, cursor='hand2', borderwidth=0,
                           activebackground=card2, activeforeground=fg,
                           command=_do_cancel)
    cancel_btn.pack(side='right', padx=(6, 0))

    entry.bind('<Return>', _do_submit)
    entry.bind('<Escape>', lambda _e: _do_cancel())

    frame = outer
    frame.update_idletasks()
    height = max(frame.winfo_reqheight(), 140)
    height = min(height, ch - 40)

    final_x = cw // 2
    final_y = ch // 2
    start_y = -(height // 2 + 20)

    item = canvas.create_window(final_x, start_y, anchor='center',
                                window=frame, width=width, height=height)

    handle = {
        'canvas': canvas, 'item': item, 'frame': frame,
        'final_x': final_x, 'final_y': final_y,
        'start_y': start_y,
        'width': width, 'height': height,
        'on_dismiss': None, 'kind': 'center',
        'arrow_item': None,
        'click_funcid': None, 'esc_funcid': None,
        'dismissing': False,
    }
    _active[id(canvas)] = handle
    _mark_just_created(handle)

    _animate_in(handle)
    _wire_dismissal(handle)

    # Focus the Entry so the user can start typing immediately. Delayed
    # until AFTER the slide-in finishes — calling focus_set on a widget
    # embedded in a tk.Canvas triggers auto-scroll-to-focus, which fought
    # the slide-in animation and dragged the surface off the top of the
    # canvas. Waiting _IN_MS + 50ms lets the surface come to rest first,
    # so the auto-scroll has nothing to chase. takefocus is restored to 1
    # here so Tab traversal and future focus operations behave normally.
    def _focus_entry_after_anim():
        try:
            entry.configure(takefocus=1)
        except Exception:
            pass
        try:
            entry.focus_set()
        except Exception:
            pass

    try:
        handle['_focus_after_id'] = canvas.after(
            _IN_MS + 50, _focus_entry_after_anim)
    except Exception:
        pass

    return handle


def _build_near(canvas, target_widget, message, actions, on_dismiss):
    canvas.update_idletasks()
    cw = max(canvas.winfo_width(), 200)
    ch = max(canvas.winfo_height(), 200)

    width = min(400, max(260, cw - 40))
    frame = _build_message_frame(canvas, message, actions, width)
    frame.update_idletasks()
    height = max(frame.winfo_reqheight(), 80)
    height = min(height, ch - 40)

    # Translate the target widget's screen rect into canvas-local coordinates.
    # Works even when the target isn't a descendant of the canvas (toolbar
    # buttons are children of the header Frame, not the canvas).
    try:
        tx_screen = target_widget.winfo_rootx()
        ty_screen = target_widget.winfo_rooty()
        tw = target_widget.winfo_width()
        th = target_widget.winfo_height()
        cx_screen = canvas.winfo_rootx()
        cy_screen = canvas.winfo_rooty()
        target_x = tx_screen - cx_screen
        target_y = ty_screen - cy_screen
    except Exception:
        target_x, target_y, tw, th = cw // 2, 0, 100, 24

    target_cx = target_x + tw // 2
    target_bottom = target_y + th
    target_top = target_y
    margin = 12

    # Prefer below; flip above if it overflows the canvas.
    place_below = (target_bottom + margin + height) <= ch
    if place_below:
        final_y = target_bottom + margin + height // 2
        arrow_apex_y = final_y - height // 2
        arrow_dir = 'up'
        slide_from = -_NEAR_SLIDE_PX
    else:
        final_y = target_top - margin - height // 2
        arrow_apex_y = final_y + height // 2
        arrow_dir = 'down'
        slide_from = _NEAR_SLIDE_PX

    # Clamp horizontal so the surface stays inside the canvas with 8px margin.
    half_w = width // 2
    final_x = max(half_w + 8, min(target_cx, cw - half_w - 8))

    start_y = final_y + slide_from

    item = canvas.create_window(final_x, start_y, anchor='center',
                                window=frame, width=width, height=height)

    # Small triangle arrow pointing at the target. Stays at its true apex
    # position throughout the animation — the surface slides in to dock against it.
    apex_x = max(final_x - half_w + 14, min(target_cx, final_x + half_w - 14))
    c_theme = _theme(canvas)
    accent = c_theme.get('accent', _FALLBACK_C['accent'])
    if arrow_dir == 'up':
        poly = (apex_x, arrow_apex_y,
                apex_x - 8, arrow_apex_y + 9,
                apex_x + 8, arrow_apex_y + 9)
    else:
        poly = (apex_x, arrow_apex_y,
                apex_x - 8, arrow_apex_y - 9,
                apex_x + 8, arrow_apex_y - 9)
    arrow_item = canvas.create_polygon(*poly, fill=accent, outline='')

    handle = {
        'canvas': canvas, 'item': item, 'frame': frame,
        'final_x': final_x, 'final_y': final_y,
        'start_y': start_y,
        'width': width, 'height': height,
        'on_dismiss': on_dismiss, 'kind': 'near',
        'arrow_item': arrow_item,
        'click_funcid': None, 'esc_funcid': None,
        'dismissing': False,
    }
    _active[id(canvas)] = handle
    _mark_just_created(handle)

    _animate_in(handle)
    _wire_dismissal(handle)
    return handle


# ─── Animation ────────────────────────────────────────────────────────

def _animate_in(handle) -> None:
    """Slide the surface from start_y to final_y with ease-out cubic."""
    canvas = handle['canvas']
    start_y = handle['start_y']
    final_y = handle['final_y']
    duration = _IN_MS
    steps = max(1, duration // _FRAME_MS)
    state = {'step': 0}

    def tick():
        if _active.get(id(canvas)) is not handle:
            return  # superseded or destroyed
        if handle.get('dismissing'):
            return  # already animating out
        state['step'] += 1
        t = state['step'] / steps
        if t > 1.0:
            t = 1.0
        eased = 1.0 - (1.0 - t) ** 3
        new_y = start_y + (final_y - start_y) * eased
        try:
            canvas.coords(handle['item'], handle['final_x'], new_y)
        except Exception:
            return
        if t < 1.0:
            handle['_anim_after_id'] = canvas.after(_FRAME_MS, tick)
        else:
            handle['_anim_after_id'] = None

    handle['_anim_after_id'] = canvas.after(_FRAME_MS, tick)


def _animate_out(handle, then=None) -> None:
    """Slide out and destroy. Optional `then` callback fires after cleanup.
    Fires on_dismiss exactly once per surface."""
    if handle is None:
        return
    if handle.get('dismissing'):
        # Already on its way out; chain `then` after cleanup.
        # v4.14.5.54: store the after-id so a force-dismiss (dismiss_active /
        # _cancel_after_ids) can cancel this queued rebuild — otherwise it
        # resurrects a surface right after the user closed one.
        if callable(then):
            try:
                aid = handle['canvas'].after(_OUT_MS + _FRAME_MS, then)
                handle.setdefault('_chained_after_ids', []).append(aid)
            except Exception:
                pass
        return
    handle['dismissing'] = True

    canvas = handle['canvas']
    start_y = handle['final_y']
    if handle['kind'] == 'center':
        end_y = -(handle['height'] // 2 + 20)
    else:
        # For near-target, reverse the slide direction it came in from.
        end_y = handle['start_y']

    duration = _OUT_MS
    steps = max(1, duration // _FRAME_MS)
    state = {'step': 0}

    def tick():
        state['step'] += 1
        t = state['step'] / steps
        if t > 1.0:
            t = 1.0
        eased = t * t * t  # ease-in cubic — accelerating departure
        new_y = start_y + (end_y - start_y) * eased
        try:
            canvas.coords(handle['item'], handle['final_x'], new_y)
        except Exception:
            pass
        if t < 1.0:
            handle['_anim_after_id'] = canvas.after(_FRAME_MS, tick)
        else:
            handle['_anim_after_id'] = None
            _destroy_handle(handle)
            if callable(then):
                try:
                    then()
                except Exception:
                    pass

    handle['_anim_after_id'] = canvas.after(_FRAME_MS, tick)


def _destroy_handle(handle) -> None:
    """Delete canvas items, destroy the embedded frame, unbind dismissal
    handlers, fire on_dismiss exactly once, clear module-level state."""
    canvas = handle['canvas']

    # Cancel any pending after() callbacks tied to this handle (animation
    # ticks, deferred focus_set). Without this, a tick scheduled for the
    # previous surface can fire after a new surface is built — Tk reuses
    # canvas item IDs, and the stale tick's canvas.coords(handle['item'],
    # ...) call lands on the new surface's item, dragging it to whatever
    # y the old animation was mid-flight at.
    _cancel_after_ids(handle)

    # Canvas items
    try:
        canvas.delete(handle['item'])
    except Exception:
        pass
    if handle.get('arrow_item') is not None:
        try:
            canvas.delete(handle['arrow_item'])
        except Exception:
            pass

    # Embedded frame
    try:
        frame = handle.get('frame')
        if frame is not None:
            frame.destroy()
    except Exception:
        pass

    # Unbind dismissal handlers. The click-outside handler is installed
    # globally via bind_all() and persists for the app's lifetime — no
    # per-surface unbind needed. Escape is per-surface.
    esc_funcid = handle.get('esc_funcid')
    if esc_funcid:
        try:
            top = canvas.winfo_toplevel()
            top.unbind('<Escape>', esc_funcid)
        except Exception:
            pass

    # Fire on_dismiss exactly once
    if not handle.get('_on_dismiss_fired'):
        handle['_on_dismiss_fired'] = True
        cb = handle.get('on_dismiss')
        if callable(cb):
            try:
                cb()
            except Exception:
                pass

    # Clear module state
    if _active.get(id(canvas)) is handle:
        del _active[id(canvas)]


# ─── Dismissal wiring (click-outside + Escape) ────────────────────────

def _ensure_global_click_handler(canvas) -> None:
    """Install a persistent app-wide <Button-1> handler used to detect clicks
    that land outside any active Teacher AI surface.

    Why bind_all and not canvas.bind:
        canvas.bind('<Button-1>') only fires for clicks on the canvas's own
        bare surface — not on embedded canvas-window widgets like the
        PanedWindow that fills almost the entire content area. That's why
        the original canvas-bind approach silently failed on the user's machine.
        bind_all fires for clicks on ANY widget in the app, after the
        widget's own bindings, so the click continues to its real target
        naturally (toolbar buttons still fire, etc).

    Why install once and never remove:
        bind_all returns a funcid, but Tk's unbind_all wipes ALL handlers
        for the sequence on the 'all' tag — destructive if other code uses
        it. The single persistent handler is a small constant cost and
        walks the _active dict (typically 0 or 1 entries) per click.
    """
    global _global_click_installed
    if _global_click_installed:
        return
    try:
        root = canvas.winfo_toplevel()
    except Exception:
        return

    def _global_click(event):
        # Walk all active surfaces and dismiss any whose bbox the click
        # landed outside of. Convert event coords from the widget that
        # received the click into canvas-local coords via screen coords.
        for _key, h in list(_active.items()):
            if h.get('dismissing'):
                continue
            if h.get('_just_created'):
                # 150ms grace window — the click that summoned this
                # surface is propagating through bind_all right now and
                # would otherwise be interpreted as a click-outside
                # dismissal.
                continue
            try:
                cv = h['canvas']
                cx = cv.winfo_rootx()
                cy = cv.winfo_rooty()
                local_x = event.x_root - cx
                local_y = event.y_root - cy
                fx, fy = h['final_x'], h['final_y']
                w, hh = h['width'], h['height']
                inside = ((fx - w // 2) <= local_x <= (fx + w // 2)
                          and (fy - hh // 2) <= local_y <= (fy + hh // 2))
            except Exception:
                continue
            if not inside:
                try:
                    _animate_out(h)
                except Exception:
                    pass

    try:
        root.bind_all('<Button-1>', _global_click, add='+')
        _global_click_installed = True
    except Exception:
        pass


def _wire_dismissal(handle) -> None:
    """Wire dismissal handlers for this surface.

    Click-outside: handled globally by _ensure_global_click_handler — a
    single bind_all on the root that walks all active surfaces. We just
    make sure it's installed.

    Escape: a per-surface binding on the toplevel. Removed when the
    surface is destroyed.
    """
    canvas = handle['canvas']
    _ensure_global_click_handler(canvas)

    def _on_escape(event):
        if _active.get(id(canvas)) is not handle:
            return
        _animate_out(handle)

    try:
        top = canvas.winfo_toplevel()
        handle['esc_funcid'] = top.bind(
            '<Escape>', _on_escape, add='+')
    except Exception:
        handle['esc_funcid'] = None


# ─── FAQ matching ─────────────────────────────────────────────────────

def match_faq_entry(user_question, faq_entries):
    """Return the best-matching FAQ entry for `user_question`, or None.

    Algorithm: count how many of each entry's `keywords` appear (case-
    insensitive substring) in the user's question. Return the entry with
    the highest count. Ties resolve to the first entry encountered (stable
    by order in faq.json). Zero matches → None.

    Intentionally simple — 15 entries don't warrant TF-IDF.
    """
    if not user_question or not faq_entries:
        return None
    q = str(user_question).lower()
    best_entry = None
    best_count = 0
    for entry in faq_entries:
        try:
            kws = entry.get('keywords') or []
        except Exception:
            kws = []
        count = 0
        for kw in kws:
            try:
                if kw and str(kw).lower() in q:
                    count += 1
            except Exception:
                pass
        if count > best_count:
            best_count = count
            best_entry = entry
    return best_entry
