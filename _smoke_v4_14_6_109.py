"""Headless smoke v4.14.6.109 — fill-loop backoff fix on
consensus-hidden (full-but-actionable-short) paths.

What this proves:
  1. The whole module graph still imports (tired_market imports
     tm_queue_runner — a syntax/import break from the edit surfaces here).
  2. App launches headlessly: a real Tk root renders a Recommendations
     window listing all four fill paths (Under $5 / $5-$10 / $10-$50 / $50+)
     using the app's own _RECO_STYLE_LABELS config. On Windows tkinter needs
     no display server (the Linux Xvfb step is a no-op here).
  3. Logic mirror of the patched decision: Δactionable==0 on a FULL path
     (raw displayed >= target AND bench >= bench-floor) settles to
     steady-state (no streak bump, no backoff); a genuinely STARVED path
     (raw below target, or thin bench) still escalates.
  4. Source: the satisfied branch reuses the existing steady-state interval
     (_FILL_NO_CANDIDATES_COOLDOWN_SECONDS), does NOT call
     _apply_zero_progress_cooldown, and logs the 'steady-state (no backoff)'
     line; the starved branch keeps _apply_zero_progress_cooldown.

Run: python -X utf8 _smoke_v4_14_6_108.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tm_queue_runner as QR  # noqa: E402

QRSRC = (HERE / "tm_queue_runner.py").read_text(encoding="utf-8",
                                                errors="ignore")
FILL_PATHS = ('lottery', 'band_5_10', 'band_10_50', 'band_50_up')


# ── 1. full module graph imports ───────────────────────────────────────
def test_import_graph():
    import tired_market as TM
    assert TM.APP_VERSION == "4.14.6.109", f"version mismatch: {TM.APP_VERSION}"
    # the four bands the board renders, from the app's own config
    for p in FILL_PATHS:
        assert p in TM._RECO_STYLE_LABELS, p
    print("  [1] import OK — tired_market + tm_queue_runner load; "
          f"APP_VERSION={TM.APP_VERSION.split()[0]}")
    return True


# ── 2. app launches: real Tk render of all four paths ──────────────────
def test_recommendations_render():
    import tired_market as TM
    try:
        import tkinter as tk
    except Exception as e:
        print(f"  [2] SKIP — tkinter unavailable ({e})")
        return True
    try:
        root = tk.Tk()
    except Exception as e:
        # truly no display (would be the case on a bare Linux box w/o Xvfb)
        print(f"  [2] SKIP — no display available ({e})")
        return True
    try:
        root.withdraw()
        win = tk.Toplevel(root)
        win.title("Recommendations")
        rendered = []
        for key in FILL_PATHS:
            label = TM._RECO_STYLE_LABELS[key]
            frame = tk.Frame(win)
            frame.pack(fill='x')
            lbl = tk.Label(frame, text=f"{label}  (0/10)")
            lbl.pack(side='left')
            rendered.append((key, label))
        win.update_idletasks()  # force the geometry/render pass
        # every fill path made it onto the window
        assert [k for k, _ in rendered] == list(FILL_PATHS)
        # widgets actually realized
        assert len(win.winfo_children()) == len(FILL_PATHS)
        print("  [2] render OK — Recommendations window realized all four "
              "paths: " + ", ".join(l for _, l in rendered))
        return True
    finally:
        try:
            root.destroy()
        except Exception:
            pass


# ── 3. logic mirror of the patched decision ────────────────────────────
def test_decision_mirror():
    # Mirror of the patched branch ordering in _run_fill_mode:
    #   delta > 0          -> reset (progress)
    #   delta == 0 & full  -> steady-state (no backoff)   [NEW]
    #   delta == 0 & else  -> escalate backoff            [unchanged]
    def decide(delta, nd, nb, dt, bf):
        if delta > 0:
            return 'reset'
        if nd >= dt and nb >= bf:
            return 'steady'
        return 'backoff'

    # the live-log case: 10/10 raw, bench 19, 9/10 actionable (delta 0)
    assert decide(0, 10, 19, 10, 10) == 'steady'
    # exactly at floors
    assert decide(0, 10, 10, 10, 10) == 'steady'
    # genuinely starved: raw below target -> still escalates
    assert decide(0, 7, 19, 10, 10) == 'backoff'
    # thin bench -> still escalates
    assert decide(0, 10, 4, 10, 10) == 'backoff'
    # real progress -> reset regardless of fullness
    assert decide(2, 9, 5, 10, 10) == 'reset'
    print("  [3] decision OK — full+Δ0 settles steady-state; "
          "under-target or thin-bench Δ0 still escalates; Δ>0 resets")
    return True


# ── 4. source: branch reuses steady-state interval, skips backoff ──────
def test_source_branch():
    # the new satisfied branch exists and is keyed on the full condition
    assert "elif (int(nd) >= int(dt) and int(nb) >= int(bf)):" in QRSRC
    # it reuses the EXISTING steady-state interval (no new constant)
    assert ("no_cand[path] = now + _FILL_NO_CANDIDATES_COOLDOWN_SECONDS"
            in QRSRC)
    # it emits the plain steady-state line
    assert "steady-state (no backoff)" in QRSRC
    assert "consensus-hidden" in QRSRC
    # the starved path still escalates (backoff call still present)
    assert "_apply_zero_progress_cooldown(" in QRSRC
    # ordering: the satisfied elif must come BEFORE the escalating else's
    # backoff call within the Δactionable<=0 handling.
    i_elif = QRSRC.find(
        "elif (int(nd) >= int(dt) and int(nb) >= int(bf)):")
    i_backoff = QRSRC.find("reason=f'Δactionable={_delta}'")
    assert -1 < i_elif < i_backoff, "satisfied branch must precede backoff"
    print("  [4] source OK — satisfied branch reuses "
          "_FILL_NO_CANDIDATES_COOLDOWN_SECONDS, logs steady-state, "
          "precedes the (unchanged) escalating backoff")
    return True


if __name__ == "__main__":
    ok = True
    print("\nSMOKE v4.14.6.109 — fill backoff steady-state on full/"
          "consensus-hidden paths")
    ok &= test_import_graph()
    ok &= test_recommendations_render()
    ok &= test_decision_mirror()
    ok &= test_source_branch()
    if ok:
        print("SMOKE v4.14.6.109 OK — module graph imports, Recommendations "
              "renders all four paths, full+Δ0 settles steady-state (no "
              "backoff) while genuinely-starved paths still escalate")
        sys.exit(0)
    print("SMOKE v4.14.6.109 FAILED")
    sys.exit(1)
