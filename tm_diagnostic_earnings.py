"""
tm_diagnostic_earnings.py — Earnings-window flag + split accuracy report

What this does:
  1. Reads data/predictions.jsonl
  2. Reads data/earnings_calendar_*.json (most recent snapshot)
  3. For each prediction with a defined window (closed predictions, or open
     BUY predictions with timeframe_days), checks whether any earnings event
     for that ticker falls inside the window.
  4. Annotates each prediction with three new fields:
       earnings_in_window   bool
       earnings_date        ISO date or null
       earnings_importance  1..5 or null
  5. Backs up predictions.jsonl and writes the annotated version back.
  6. Computes split accuracy (with vs. without earnings in window), broken
     down by path / model / timeframe bucket.
  7. Detects ticker concentration clusters (multiple closed predictions on
     same ticker that resolved on the same day -- model-vote duplicates).
  8. Writes a markdown report to data/diagnostic_earnings_<timestamp>.md

This script does NOT change behavior of the running app. It only adds metadata.
The recommendation engine, closer, and prediction engine all continue to run
unchanged. The flag is for measurement only.

Idempotent: running it twice updates the flag fields in place; it does not
duplicate predictions or compound the changes.

Run via RUN_DIAGNOSTIC_EARNINGS.bat or directly:
    python tm_diagnostic_earnings.py
"""

# Note: as of v4.14.1 (2026-05-08), this script reads a STATIC Polygon
# snapshot from data/earnings_calendar_*.json. The runtime earnings path
# in tm_discover._load_earnings_calendar moved to live Finnhub data via
# the router. This script does NOT use the live source; if you want a
# diagnostic against current data, refactor find_earnings_in_window to
# call tm_data_router.get_router().fetch('earnings', ...).

import json
import os
import sys
import shutil
import glob
from datetime import datetime, date, timedelta
from collections import defaultdict, Counter

CLOSED_STATUSES = {"target_hit", "stop_hit", "expired", "closed"}


def find_install_dir():
    """Return install dir path. Script lives in install root."""
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isfile(os.path.join(here, "tired_market.py")):
        return here
    # Fallback: D:\TiredMarket\
    fallback = r"D:\TiredMarket"
    if os.path.isfile(os.path.join(fallback, "tired_market.py")):
        return fallback
    print("ERROR: Could not locate Tired Market install directory.")
    print("Place this script in D:\\TiredMarket\\ and re-run.")
    sys.exit(1)


def load_earnings_calendar(data_dir):
    """Load most recent earnings calendar snapshot. Returns dict ticker -> [events]."""
    pattern = os.path.join(data_dir, "earnings_calendar_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"ERROR: No earnings calendar found at {pattern}")
        print("Expected file like data/earnings_calendar_20260505.json")
        sys.exit(1)
    chosen = files[-1]
    with open(chosen) as f:
        cal = json.load(f)
    by_ticker = defaultdict(list)
    for e in cal["events"]:
        by_ticker[e["ticker"]].append({
            "date": date.fromisoformat(e["date"]),
            "importance": int(e["importance"]),
            "fiscal_period": e.get("fiscal_period", ""),
        })
    return chosen, cal, by_ticker


def parse_dt(s):
    """Parse ISO timestamp tolerantly, return date."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        try:
            return date.fromisoformat(s[:10])
        except Exception:
            return None


def prediction_window(p):
    """
    Return (start_date, end_date) for a prediction, or (None, None) if it
    doesn't have a meaningful window.

    Closed predictions: (timestamp_date, closed_at_date)
    Open BUY w/ timeframe: (timestamp_date, timestamp_date + timeframe_days)
    Everything else: (None, None) -- AVOID/NO_CALL/HOLD without timeframe
    are opinions, not trades, and don't get flagged.
    """
    status = p.get("status", "")
    start = parse_dt(p.get("timestamp"))
    if not start:
        return None, None

    if status in CLOSED_STATUSES:
        end = parse_dt(p.get("closed_at"))
        if not end:
            tf = p.get("timeframe_days") or 0
            if tf:
                end = start + timedelta(days=tf)
            else:
                return None, None
        return start, end

    # Open
    if status == "open":
        direction = p.get("direction", "")
        tf = p.get("timeframe_days") or 0
        # Only actionable directions with a defined timeframe get a window
        if direction in ("BUY", "HOLD") and tf > 0:
            return start, start + timedelta(days=tf)

    return None, None


def find_earnings_in_window(ticker, start_d, end_d, by_ticker, min_importance=1):
    """First earnings event for ticker within [start, end]. Returns event or None."""
    for ev in by_ticker.get(ticker, []):
        if ev["importance"] < min_importance:
            continue
        if start_d <= ev["date"] <= end_d:
            return ev
    return None


def annotate_predictions(predictions, by_ticker):
    """Add earnings_in_window / earnings_date / earnings_importance fields in place."""
    flagged = 0
    skipped = 0
    for p in predictions:
        start, end = prediction_window(p)
        if start is None:
            # Not a trade-window prediction. Clear any stale flag.
            p["earnings_in_window"] = None
            p["earnings_date"] = None
            p["earnings_importance"] = None
            skipped += 1
            continue
        ev = find_earnings_in_window(p["ticker"], start, end, by_ticker)
        if ev:
            p["earnings_in_window"] = True
            p["earnings_date"] = ev["date"].isoformat()
            p["earnings_importance"] = ev["importance"]
            flagged += 1
        else:
            p["earnings_in_window"] = False
            p["earnings_date"] = None
            p["earnings_importance"] = None
    return flagged, skipped


def tf_bucket(d):
    if d is None:
        return "no timeframe"
    if d <= 7:
        return "1. <=7d"
    if d <= 21:
        return "2. 8-21d"
    if d <= 45:
        return "3. 22-45d"
    if d <= 90:
        return "4. 46-90d"
    return "5. >90d"


def compute_split(closed):
    """Compute hit-rate split by earnings flag for a list of closed predictions."""
    with_e = [p for p in closed if p.get("earnings_in_window")]
    no_e = [p for p in closed if p.get("earnings_in_window") is False]
    def hit_rate(group):
        if not group:
            return 0.0, 0, 0
        hits = sum(1 for p in group if p["status"] == "target_hit")
        misses = sum(1 for p in group if p["status"] == "stop_hit")
        total = hits + misses
        rate = (hits / total * 100) if total else 0.0
        return rate, hits, total
    return {
        "with_earnings": (with_e, hit_rate(with_e)),
        "no_earnings": (no_e, hit_rate(no_e)),
        "all": (closed, hit_rate(closed)),
    }


def detect_concentration_clusters(closed):
    """Find closed predictions that resolved on the same day for the same ticker."""
    by_key = defaultdict(list)
    for p in closed:
        end_d = parse_dt(p.get("closed_at"))
        if end_d:
            by_key[(p["ticker"], end_d.isoformat())].append(p)
    clusters = {k: v for k, v in by_key.items() if len(v) > 1}
    return clusters


def write_report(report_path, install_dir, cal_file, cal_meta, predictions,
                 flagged, skipped, calendar_by_ticker):
    open_buy = [p for p in predictions
                if p.get("status") == "open"
                and p.get("direction") == "BUY"
                and p.get("timeframe_days")]
    closed = [p for p in predictions if p.get("status") in CLOSED_STATUSES]

    open_with_e = [p for p in open_buy if p.get("earnings_in_window")]
    open_with_major = [p for p in open_buy if p.get("earnings_importance") and p["earnings_importance"] >= 4]

    split = compute_split(closed)
    clusters = detect_concentration_clusters(closed)

    lines = []
    L = lines.append
    L(f"# Earnings-window diagnostic")
    L(f"")
    L(f"**Run:** {datetime.now().isoformat(timespec='seconds')}")
    L(f"**Install:** `{install_dir}`")
    L(f"**Calendar source:** `{os.path.basename(cal_file)}` ({cal_meta.get('source','?')})")
    L(f"**Calendar snapshot date:** {cal_meta.get('snapshot_date','?')}")
    L(f"**Calendar coverage:** {cal_meta.get('event_count','?')} events on {len(cal_meta.get('tickers_covered',[]))} tickers")
    L(f"")
    L(f"## Annotation summary")
    L(f"")
    L(f"- Total predictions in file: **{len(predictions)}**")
    L(f"- Predictions flagged with earnings in window: **{flagged}**")
    L(f"- Predictions skipped (no trade window — AVOID / NO_CALL / etc): **{skipped}**")
    L(f"")

    # ---- Closed-side: the smoking gun -----------------------------------
    L(f"## Closed predictions: split accuracy")
    L(f"")
    rate_all, hits_all, n_all = split["all"][1]
    rate_we, hits_we, n_we = split["with_earnings"][1]
    rate_ne, hits_ne, n_ne = split["no_earnings"][1]
    L(f"| Bucket | Hits | Total | Hit rate |")
    L(f"|---|---|---|---|")
    L(f"| With earnings in window | {hits_we} | {n_we} | **{rate_we:.1f}%** |")
    L(f"| No earnings in window   | {hits_ne} | {n_ne} | **{rate_ne:.1f}%** |")
    L(f"| All (headline)          | {hits_all} | {n_all} | **{rate_all:.1f}%** |")
    L(f"")
    L(f"The 'no earnings' row is the cleanest read of the system's actual stock-picking ability,")
    L(f"because the predictions in that bucket weren't disturbed by a binary catalyst the system")
    L(f"couldn't see. Sample size is small -- treat both rows as directional, not conclusive.")
    L(f"")

    # ---- Per-path split -------------------------------------------------
    L(f"## Closed predictions by path")
    L(f"")
    L(f"| Path | All | Earnings-window | Non-earnings | Non-earn hit rate |")
    L(f"|---|---|---|---|---|")
    paths = sorted(set(p.get("path", "?") for p in closed))
    for path in paths:
        path_closed = [p for p in closed if p.get("path") == path]
        path_we = [p for p in path_closed if p.get("earnings_in_window")]
        path_ne = [p for p in path_closed if p.get("earnings_in_window") is False]
        h = sum(1 for p in path_ne if p["status"] == "target_hit")
        t = len(path_ne)
        rate_str = f"{100*h/t:.1f}%" if t else "n/a"
        L(f"| {path} | {len(path_closed)} | {len(path_we)} | {len(path_ne)} | {h}/{t} = {rate_str} |")
    L(f"")

    # ---- Per-model split ------------------------------------------------
    L(f"## Closed predictions by model")
    L(f"")
    L(f"| Model | All | Earnings-window | Non-earnings | Non-earn hit rate |")
    L(f"|---|---|---|---|---|")
    models = sorted(set(p.get("model", "?") for p in closed))
    for m in models:
        m_closed = [p for p in closed if p.get("model") == m]
        m_we = [p for p in m_closed if p.get("earnings_in_window")]
        m_ne = [p for p in m_closed if p.get("earnings_in_window") is False]
        h = sum(1 for p in m_ne if p["status"] == "target_hit")
        t = len(m_ne)
        rate_str = f"{100*h/t:.1f}%" if t else "n/a"
        L(f"| {m} | {len(m_closed)} | {len(m_we)} | {len(m_ne)} | {h}/{t} = {rate_str} |")
    L(f"")

    # ---- Concentration warning ------------------------------------------
    L(f"## Ticker concentration in closed sample")
    L(f"")
    if clusters:
        L(f"Found **{len(clusters)} cluster(s)** of multiple closed predictions on the same")
        L(f"ticker resolved on the same date. These are almost certainly model-vote duplicates")
        L(f"of one effective trade. They inflate the closed sample size and double-count the")
        L(f"impact of any single-ticker event.")
        L(f"")
        L(f"| Ticker | Resolved | Count | Statuses |")
        L(f"|---|---|---|---|")
        for (ticker, dstr), preds in sorted(clusters.items(), key=lambda x: -len(x[1])):
            statuses = Counter(p["status"] for p in preds)
            stat_str = ", ".join(f"{s}={c}" for s, c in statuses.items())
            L(f"| {ticker} | {dstr} | {len(preds)} | {stat_str} |")
        L(f"")
        L(f"**Recommendation:** before publishing any per-path or per-model accuracy number,")
        L(f"decide how to handle these. Easiest: collapse clusters to one prediction (highest")
        L(f"confidence, or the first one chronologically). Also worth deciding whether the")
        L(f"prediction engine should refuse to make a new prediction on a ticker that already")
        L(f"has an open active prediction in the same path.")
    else:
        L(f"No clusters detected.")
    L(f"")

    # ---- Open exposure (forward looking) --------------------------------
    L(f"## Open BUY predictions: forward exposure")
    L(f"")
    pct_any = 100 * len(open_with_e) / len(open_buy) if open_buy else 0
    pct_major = 100 * len(open_with_major) / len(open_buy) if open_buy else 0
    L(f"- Total open BUY w/ timeframe: **{len(open_buy)}**")
    L(f"- With earnings in resolution window (any importance): **{len(open_with_e)} ({pct_any:.1f}%)**")
    L(f"- With MAJOR earnings (importance 4-5): **{len(open_with_major)} ({pct_major:.1f}%)**")
    L(f"")
    L(f"### Forward exposure by path")
    L(f"")
    L(f"| Path | Total open | Earnings-exposed | % |")
    L(f"|---|---|---|---|")
    open_paths = sorted(set(p.get("path", "?") for p in open_buy))
    for path in open_paths:
        path_open = [p for p in open_buy if p.get("path") == path]
        path_open_e = [p for p in path_open if p.get("earnings_in_window")]
        pct = 100 * len(path_open_e) / len(path_open) if path_open else 0
        L(f"| {path} | {len(path_open)} | {len(path_open_e)} | {pct:.1f}% |")
    L(f"")
    L(f"### Forward exposure by timeframe")
    L(f"")
    L(f"| Timeframe | Total open | Earnings-exposed | % |")
    L(f"|---|---|---|---|")
    buckets = sorted(set(tf_bucket(p.get("timeframe_days")) for p in open_buy))
    for b in buckets:
        b_open = [p for p in open_buy if tf_bucket(p.get("timeframe_days")) == b]
        b_open_e = [p for p in b_open if p.get("earnings_in_window")]
        pct = 100 * len(b_open_e) / len(b_open) if b_open else 0
        L(f"| {b} | {len(b_open)} | {len(b_open_e)} | {pct:.1f}% |")
    L(f"")

    # ---- Tickers not covered by calendar --------------------------------
    pred_tickers = set(p["ticker"] for p in predictions if p.get("ticker"))
    cal_tickers = set(calendar_by_ticker.keys())
    uncovered = pred_tickers - cal_tickers
    if uncovered:
        L(f"## Tickers in predictions but NOT in calendar snapshot")
        L(f"")
        L(f"These tickers have predictions but no earnings data in the loaded calendar. The")
        L(f"earnings_in_window flag for these is conservatively False (could not check).")
        L(f"")
        L(f"To fix: pull a fresh earnings calendar snapshot covering these tickers.")
        L(f"")
        L(f"Tickers: {', '.join(sorted(uncovered))}")
        L(f"")

    L(f"## Notes")
    L(f"")
    L(f"- This script only adds annotation. The running app's behavior is unchanged.")
    L(f"- The flag is conservative: importance 1-5 events are all flagged. Many importance-1")
    L(f"  and 2 events have negligible price impact in practice. If you want a stricter view,")
    L(f"  re-run the diagnostic with a higher minimum importance.")
    L(f"- If you've added new tickers to the universe since the calendar snapshot, those")
    L(f"  tickers' predictions will not be flagged (no earnings data available). Pull a fresh")
    L(f"  snapshot when meaningful new tickers appear.")
    L(f"")

    with open(report_path, "w") as f:
        f.write("\n".join(lines))


def main():
    install_dir = find_install_dir()
    data_dir = os.path.join(install_dir, "data")
    pred_path = os.path.join(data_dir, "predictions.jsonl")

    if not os.path.isfile(pred_path):
        print(f"ERROR: predictions.jsonl not found at {pred_path}")
        sys.exit(1)

    print(f"[1/5] Loading earnings calendar...")
    cal_file, cal_meta, by_ticker = load_earnings_calendar(data_dir)
    print(f"      Loaded {cal_meta.get('event_count','?')} events from {os.path.basename(cal_file)}")

    print(f"[2/5] Loading predictions...")
    predictions = []
    with open(pred_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            predictions.append(json.loads(line))
    print(f"      Loaded {len(predictions)} predictions")

    print(f"[3/5] Annotating predictions with earnings_in_window flag...")
    flagged, skipped = annotate_predictions(predictions, by_ticker)
    print(f"      Flagged {flagged}, skipped {skipped} (no trade window)")

    print(f"[4/5] Writing annotated predictions...")
    backup_path = pred_path + f".pre_earnings_diag.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
    shutil.copy2(pred_path, backup_path)
    print(f"      Backup saved: {os.path.basename(backup_path)}")
    with open(pred_path, "w") as f:
        for p in predictions:
            f.write(json.dumps(p) + "\n")
    print(f"      predictions.jsonl rewritten with flags")

    print(f"[5/5] Generating report...")
    report_path = os.path.join(data_dir, f"diagnostic_earnings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    write_report(report_path, install_dir, cal_file, cal_meta, predictions,
                 flagged, skipped, by_ticker)
    print(f"      Report: {report_path}")

    print("")
    print("DONE.")
    print(f"Open {report_path} to read findings.")


if __name__ == "__main__":
    main()
