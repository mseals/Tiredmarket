"""
Tired Market — Phase 2C: Discover, predictions, AI track record.

This module owns four things:

1. **Watchlist**: persistent list of tickers the user wants the AI to analyze.
   File: data/watchlist.json. Manual input, manual edit, no auto-population.

2. **Universe**: the broader pool to scan when looking for new ideas.
   Currently sourced from the Russell 2000 (IWM ETF holdings, refreshed
   weekly to a local cache). Falls back to a hardcoded curated list if
   the network fetch fails.

3. **Predictions**: every AI analysis produces a structured prediction
   (direction / buy zone / target / stop / timeframe / confidence). All
   predictions get logged to data/predictions.jsonl with timestamps.

4. **Track record**: outcome tracking. When target/stop/timeframe is
   reached (or position is sold), the prediction record gets closed
   with the actual outcome. Aggregated stats power the Track Record view.

Design principles (settled in conversation with the user):

- Manual trigger only. No scheduled discovery runs.
- Honest framing in output language: "best of today's scan" not "top picks"
- No buy buttons, no broker integration — just suggestions the user acts on
  separately.
- Track record diagnoses whether the AI is useful, NOT predicts the next
  trade. Roulette principle: past results inform whether you're playing
  a winnable version of the game.
- The AI sees its own track record when making new calls (injected into
  prompts). Whether this actually improves accuracy is empirical — but
  it costs nothing and the meta-data is useful for the user either way.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional


# ─── Watchlist ────────────────────────────────────────────────────────

class Watchlist:
    """Persistent list of tickers the user has flagged for analysis.

    Schema (data/watchlist.json):
    {
        "tickers": [
            {"ticker": "AAPL", "added_at": "...", "notes": "earnings next week"},
            {"ticker": "BLNK", "added_at": "...", "notes": "EV charging buildout"}
        ]
    }
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"tickers": []}
        try:
            with open(self.path) as f:
                d = json.load(f)
            if not isinstance(d, dict) or "tickers" not in d:
                return {"tickers": []}
            return d
        except Exception:
            return {"tickers": []}

    def save(self):
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, 'w') as f:
                    json.dump(self._data, f, indent=2)
            except Exception:
                pass

    @property
    def tickers(self) -> list[dict]:
        return list(self._data.get("tickers", []))

    def add(self, ticker: str, notes: str = "") -> bool:
        """Add a ticker to the watchlist. Returns True if added, False if
        already present (idempotent)."""
        ticker = ticker.strip().upper()
        if not ticker or not re.match(r'^[A-Z][A-Z0-9.\-]{0,9}$', ticker):
            return False
        with self._lock:
            existing = {t.get("ticker", "").upper() for t in self._data["tickers"]}
            if ticker in existing:
                return False
            self._data["tickers"].append({
                "ticker": ticker,
                "added_at": datetime.now().isoformat(),
                "notes": notes.strip(),
            })
        self.save()
        # v4.14.4.3 (2026-05-15): user-signal trigger. Fires only on
        # actual new add (not duplicate). Failure here is non-fatal —
        # watchlist functionality must not break if the trigger
        # system is misconfigured. Module-level _app_ref in
        # tm_event_triggers (set at App init) supplies the app
        # reference; cfg['analysis_path'] supplies the target path.
        try:
            import tm_event_triggers as _tet
            _tet.record_user_signal(
                ticker, 'watchlist_add',
                context_str=(notes.strip()[:80] if notes else ''))
        except Exception:
            pass
        return True

    def remove(self, ticker: str) -> bool:
        ticker = ticker.strip().upper()
        with self._lock:
            before = len(self._data["tickers"])
            self._data["tickers"] = [
                t for t in self._data["tickers"]
                if t.get("ticker", "").upper() != ticker
            ]
            removed = len(self._data["tickers"]) < before
        if removed:
            self.save()
        return removed

    def update_notes(self, ticker: str, notes: str) -> bool:
        ticker = ticker.strip().upper()
        with self._lock:
            for t in self._data["tickers"]:
                if t.get("ticker", "").upper() == ticker:
                    t["notes"] = notes.strip()
                    self.save()
                    return True
        return False

    def replace_all(self, entries: list[dict]):
        """Bulk replace the watchlist. Used by the textarea editor."""
        with self._lock:
            self._data["tickers"] = list(entries)
        self.save()

    def parse_text_block(self, text: str) -> list[dict]:
        """Parse a multi-line text block into watchlist entries.
        Format per line:
            TICKER
            TICKER:notes
            TICKER  notes
        Blank lines and comments (#...) ignored.
        """
        entries = []
        seen = set()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # Split on : or whitespace (first occurrence)
            ticker = ""
            notes = ""
            if ':' in line:
                ticker, notes = line.split(':', 1)
            else:
                parts = re.split(r'\s+', line, maxsplit=1)
                ticker = parts[0]
                if len(parts) > 1:
                    notes = parts[1]
            ticker = ticker.strip().upper()
            notes = notes.strip()
            if not re.match(r'^[A-Z][A-Z0-9.\-]{0,9}$', ticker):
                continue
            if ticker in seen:
                continue
            seen.add(ticker)
            # Preserve added_at if this ticker was already in the list
            existing_added = None
            for t in self._data.get("tickers", []):
                if t.get("ticker", "").upper() == ticker:
                    existing_added = t.get("added_at")
                    break
            entries.append({
                "ticker": ticker,
                "added_at": existing_added or datetime.now().isoformat(),
                "notes": notes,
            })
        return entries


# ─── Universe ─────────────────────────────────────────────────────────

# Curated fallback list — used if we can't fetch IWM holdings dynamically.
# These are well-known small/mid-cap tickers across sectors. Not Russell
# 2000 strict membership, just "stuff worth scanning." the user can extend.
FALLBACK_UNIVERSE = [
    # EV / Energy
    "BLNK", "CHPT", "EVGO", "RIVN", "LCID", "NIO", "FSR", "WKHS", "RIDE",
    "NKLA", "GOEV", "HYZN", "PLUG", "BE", "BLDP", "FCEL",
    # Biotech (small/mid cap)
    "RIGL", "ARCT", "VKTX", "AMRX", "COMP", "OCGN", "VYNE", "BNGO",
    "IBIO", "ATOS", "INO", "VBLT", "GERN", "CRDF", "SAVA", "OPTN",
    # Crypto / fintech
    "RIOT", "MARA", "HUT", "BTBT", "CLSK", "BITF", "HIVE",
    "SOFI", "UPST", "HOOD", "MQ", "PYPL", "AFRM",
    # Tech / SaaS small/mid cap
    "PLTR", "RBLX", "NET", "DDOG", "SNOW", "MDB", "TEAM", "U", "FROG",
    "AI", "PATH", "DOMO", "CFLT", "S", "ESTC", "RPD", "ZS",
    # Cannabis
    "TLRY", "ACB", "CGC", "CRON", "VFF", "OGI", "SNDL", "HEXO",
    # Solar
    "RUN", "NOVA", "ENPH", "SEDG", "FSLR", "JKS", "CSIQ", "DQ",
    # Shipping / logistics
    "ZIM", "GOGL", "EGLE", "SBLK", "CMRE", "DAC", "GNK",
    # Semiconductor (smaller names)
    "MRVL", "ON", "WOLF", "MPWR", "AMBA", "POWI", "SLAB", "CRUS",
    # Other interesting small caps
    "BBBY", "GME", "AMC", "BB", "NOK", "BKKT", "DWAC",
    "BIG", "TUEM", "EXPR", "GOEV", "MULN", "ATER", "PROG",
    # Retail / consumer
    "FIGS", "WRBY", "REAL", "POSH", "RVLV", "DOCN",
]


# v4.15.0 Step 22a: hardcoded universes for sources that don't publish a
# predictable iShares-style CSV URL.

# DOW 30 — Dow Jones Industrial Average. Membership changes maybe once every
# few years. Last reshuffle relevant here: NVDA + SHW added, INTC + DOW Inc
# removed in November 2024. Update this list when the next reshuffle lands.
_DOW_30 = [
    'AAPL', 'AMGN', 'AMZN', 'AXP', 'BA',   'CAT', 'CRM', 'CSCO', 'CVX', 'DIS',
    'GS',   'HD',   'HON',  'IBM', 'JNJ',  'JPM', 'KO',  'MCD',  'MMM', 'MRK',
    'MSFT', 'NKE',  'NVDA', 'PG',  'SHW',  'TRV', 'UNH', 'V',    'VZ',  'WMT',
]

# NASDAQ-100 — Invesco QQQ holdings, top 100 NASDAQ non-financial names.
# Membership reviewed annually in December. The list below is current to the
# December 2024 reconstitution. Update annually as needed; staleness here only
# affects which tickers get cache-filled when the user picks NASDAQ-100.
_NASDAQ_100 = [
    'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', 'GOOGL', 'GOOG', 'AVGO', 'TSLA', 'COST',
    'NFLX', 'AMD',  'PEP',  'ASML', 'TMUS', 'ADBE',  'LIN',  'CSCO', 'AZN',  'QCOM',
    'TXN',  'INTC', 'INTU', 'CMCSA','HON',  'AMGN',  'ISRG', 'AMAT', 'BKNG', 'PDD',
    'VRTX', 'ADP',  'PANW', 'MU',   'GILD', 'SBUX',  'LRCX', 'REGN', 'MDLZ', 'KLAC',
    'ADI',  'MELI', 'CDNS', 'SNPS', 'CRWD', 'CTAS',  'PYPL', 'MAR',  'CSX',  'MRVL',
    'ABNB', 'WDAY', 'ORLY', 'ROP',  'CHTR', 'NXPI',  'CEG',  'ADSK', 'PCAR', 'FTNT',
    'MNST', 'DASH', 'KDP',  'PAYX', 'AEP',  'CPRT',  'ROST', 'ODFL', 'FAST', 'KHC',
    'EA',   'TTD',  'BKR',  'EXC',  'XEL',  'CTSH',  'GEHC', 'IDXX', 'CSGP', 'DDOG',
    'VRSK', 'LULU', 'TEAM', 'CCEP', 'FANG', 'AZPN',  'ON',   'ZS',   'DXCM', 'TTWO',
    'BIIB', 'MDB',  'ANSS', 'CDW',  'ARM',  'WBD',   'GFS',  'MRNA', 'ILMN', 'WBA',
]

class Universe:
    """The universe to scan in Discover. Supports multiple sources
    (S&P 500, Russell 1000, Russell 2000, Nasdaq 100, Total Market) with
    each cached separately. Switching between sources after the first
    fetch is instant.

    Cache files: data/universe_{source_key}.json
        {
            "tickers": ["AAA", "BBB", ...],
            "fetched_at": "2026-04-27T...",
            "source": "iwm_holdings" or "fallback"
        }

    Sources:
        ivv  - S&P 500 (~500 names, ~30 changes/year)
        iwb  - Russell 1000 (~1000 names, June reconstitution)
        iwm  - Russell 2000 (~2000 names, June reconstitution)
        qqq  - Nasdaq 100 (~100 names, December rebalance)
        vti  - Total Market (~3500 names, frequent IPO additions)

    Most recent S&P 500 + Russell 2000 are most useful for retail. Nasdaq
    100 is tech-heavy. Total Market is comprehensive but slow.
    """

    REFRESH_DAYS = 7

    # Each source has a label, an iShares CSV URL, and a cache file suffix.
    # The CSV format from iShares is consistent across funds.
    SOURCES = {
        # v4.10.1: IWV (Russell 3000) is the SUPERSET. Other universes are
        # subsets — we filter IWV's snapshot by membership rather than
        # fetching them separately. The 'subset_of' field marks this.
        # IVV/IWB are still fetched (small lists, used to determine
        # membership). IWM is COMPUTED — anything in IWV but not IWB.
        'iwv': {
            'label': 'Russell 3000 (IWV)',
            'short': 'Russell 3000',
            'url': ("https://www.ishares.com/us/products/239714/ishares-russell-"
                     "3000-etf/1467271812596.ajax?fileType=csv&fileName=IWV"
                     "_holdings&dataType=fund"),
            'is_superset': True,  # v4.10.1: every other universe filters this
        },
        'iwb': {
            'label': 'Russell 1000 (IWB)',
            'short': 'Russell 1000',
            'url': ("https://www.ishares.com/us/products/239707/ishares-russell-"
                     "1000-etf/1467271812596.ajax?fileType=csv&fileName=IWB"
                     "_holdings&dataType=fund"),
            'subset_of': 'iwv',  # filtering rule: ticker in iwb_set
        },
        'iwm': {
            'label': 'Russell 2000 (IWM)',
            'short': 'Russell 2000',
            'url': ("https://www.ishares.com/us/products/239710/ishares-russell-"
                     "2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM"
                     "_holdings&dataType=fund"),
            'subset_of': 'iwv',  # filtering rule: in iwv but NOT in iwb
            'computed_from_membership': True,  # v4.10.1: skip explicit fetch
        },
        'ivv': {
            'label': 'S&P 500 (IVV)',
            'short': 'S&P 500',
            'url': ("https://www.ishares.com/us/products/239726/ishares-core-"
                     "sp-500-etf/1467271812596.ajax?fileType=csv&fileName=IVV"
                     "_holdings&dataType=fund"),
            'subset_of': 'iwv',  # filtering rule: ticker in ivv_set
        },
        # v4.10.1: qqq and vti dropped from the dropdown — qqq is
        # mostly a subset of ivv anyway, vti is essentially identical
        # to iwv. Keeping the universe choice simple. Old configs
        # pointing at qqq/vti get auto-migrated to iwv on load.
        #
        # v4.15.0 Step 22a: three new sources for the rebuilt picker.
        # - 'dow': hardcoded 30-name list (membership stable for years).
        # - 'nasdaq100': hardcoded ~100-name list (top NASDAQ-100 by cap).
        #   Invesco QQQ doesn't publish a predictable CSV URL like iShares,
        #   so we ship a known-good list instead of relying on a fragile
        #   public scrape.
        # - 'itot': iShares Core S&P Total U.S. Stock Market ETF; fetched
        #   via the same CSV path as IVV/IWB/IWM/IWV.
        # Both hardcoded sources set 'hardcoded_tickers' which _fetch_one
        # checks before its URL-fetch branch.
        'dow': {
            'label': 'Dow 30',
            'short': 'Dow 30',
            'hardcoded_tickers': _DOW_30,
        },
        'nasdaq100': {
            'label': 'NASDAQ-100',
            'short': 'NASDAQ-100',
            'hardcoded_tickers': _NASDAQ_100,
        },
        'itot': {
            'label': 'US Total Market (ITOT)',
            'short': 'ITOT',
            'url': ("https://www.ishares.com/us/products/239724/ishares-core-"
                     "sp-total-us-stock-market-etf/1467271812596.ajax?"
                     "fileType=csv&fileName=ITOT_holdings&dataType=fund"),
        },
    }

    DEFAULT_SOURCE = 'iwv'  # v4.10.1: superset is the new default
    PRIMARY_SOURCE = 'iwv'  # the one we always fetch fully

    def __init__(self, base_dir: Path, current_source: str = DEFAULT_SOURCE):
        """base_dir is the data folder; cache files live there as
        universe_{source}.json. current_source is the active selection."""
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._caches: dict[str, dict] = {}  # source_key -> cache dict
        self.current_source = current_source if current_source in self.SOURCES \
            else self.DEFAULT_SOURCE

        # Migrate the old single universe.json to universe_iwm.json if present
        self._migrate_legacy()

        # Load whatever's already cached on disk
        for key in self.SOURCES:
            self._caches[key] = self._load_one(key)

    def _migrate_legacy(self):
        """If a v4.8 universe.json exists, move it to universe_iwm.json."""
        legacy = self.base_dir / "universe.json"
        target = self.base_dir / "universe_iwm.json"
        if legacy.exists() and not target.exists():
            try:
                legacy.rename(target)
            except Exception:
                pass

    def _cache_path(self, source: str) -> Path:
        return self.base_dir / f"universe_{source}.json"

    def _load_one(self, source: str) -> dict:
        p = self._cache_path(source)
        if not p.exists():
            return {"tickers": [], "fetched_at": None, "source": None}
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return {"tickers": [], "fetched_at": None, "source": None}

    def _save_one(self, source: str):
        p = self._cache_path(source)
        with self._lock:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, 'w') as f:
                    json.dump(self._caches[source], f, indent=2)
            except Exception:
                pass

    def set_source(self, source: str):
        """Switch the active universe source. Saves nothing; persistence
        is handled by the app-level config.

        v4.10.1: Migrate old qqq/vti selections to iwv automatically since
        those sources were dropped from SOURCES."""
        if source in self.SOURCES:
            self.current_source = source
        else:
            # Old config might have qqq/vti — silently migrate to iwv
            self.current_source = self.DEFAULT_SOURCE

    @property
    def tickers(self) -> list[str]:
        """Tickers for the currently selected source.

        v4.10.1: For subset universes (IVV, IWB, IWM), this filters the
        IWV cache by membership rather than returning a separately-fetched
        list. Result: switching universes is instant — no re-fetch.

        IWV (the superset) returns all cached tickers as-is.
        IWM is computed: tickers in IWV that are NOT in IWB.
        IVV/IWB return their own cached lists (fetched for membership).
        """
        info = self.SOURCES.get(self.current_source, {})
        if info.get('is_superset'):
            # IWV — return its full cached list
            return list(self._caches.get(self.current_source, {})
                          .get("tickers", []))

        if info.get('computed_from_membership'):
            # IWM — tickers in IWV that are NOT in IWB
            iwv_tickers = self._caches.get('iwv', {}).get('tickers', [])
            iwb_set = set(self._caches.get('iwb', {}).get('tickers', []))
            if not iwv_tickers:
                # IWV not yet fetched. Backward compat: if we have an
                # old universe_iwm.json from pre-v4.10.1, use it as a
                # one-time bridge until the next refresh fetches IWV.
                legacy_iwm = self._caches.get('iwm', {}).get('tickers', [])
                if legacy_iwm:
                    return list(legacy_iwm)
                # Nothing cached. Refresh will fix it.
                return []
            if not iwb_set:
                # No IWB membership data — fall back to entire IWV
                # (better to over-include than return nothing)
                return list(iwv_tickers)
            return [t for t in iwv_tickers if t not in iwb_set]

        if info.get('subset_of'):
            # IVV or IWB — return its own cached list (which is also
            # the membership filter for this universe)
            return list(self._caches.get(self.current_source, {})
                          .get("tickers", []))

        # Fallback for unknown sources
        return list(self._caches.get(self.current_source, {})
                      .get("tickers", []))

    def membership_for(self, ticker: str) -> list[str]:
        """v4.10.1: Return the list of universe keys this ticker belongs to.

        Used by snapshot writers so each ticker carries membership tags,
        and by the unified Scan filter to slice a snapshot by universe.
        """
        ticker = ticker.upper()
        out = []
        # IWV — superset, every cached ticker is in it
        if ticker in set(self._caches.get('iwv', {}).get('tickers', [])):
            out.append('iwv')
        # IWB — large caps
        in_iwb = ticker in set(self._caches.get('iwb', {}).get('tickers', []))
        if in_iwb:
            out.append('iwb')
        # IWM — small caps (in IWV but not IWB)
        if 'iwv' in out and not in_iwb:
            out.append('iwm')
        # IVV — S&P 500 (subset of IWB)
        if ticker in set(self._caches.get('ivv', {}).get('tickers', [])):
            out.append('ivv')
        return out

    @property
    def source(self) -> str:
        return self._caches.get(self.current_source, {}).get("source", "unknown")

    @property
    def fetched_at(self) -> Optional[datetime]:
        s = self._caches.get(self.current_source, {}).get("fetched_at")
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None

    def needs_refresh(self) -> bool:
        """v4.10.1: Check IWV (the superset) staleness, not the current
        source. Subset universes are derived from IWV — if IWV is fresh,
        all derived views are fresh too."""
        cache = self._caches.get(self.PRIMARY_SOURCE, {})
        if not cache.get("tickers"):
            return True
        last_str = cache.get("fetched_at")
        if not last_str:
            return True
        try:
            last = datetime.fromisoformat(last_str)
        except Exception:
            return True
        return (datetime.now() - last).days >= self.REFRESH_DAYS

    def source_label(self) -> str:
        return self.SOURCES.get(self.current_source, {}).get(
            'label', self.current_source)

    def refresh(self, log_fn: Callable[[str], None] | None = None) -> bool:
        """v4.10.1: Fetch IWV (the superset) PLUS IVV and IWB (for membership
        filtering). Then we can serve any subset universe instantly without
        re-fetching.

        The previous behavior fetched only the currently-selected source.
        That meant switching from IVV to IWV cost a full re-fetch of the
        bigger list — a 30-60 second wait. Now: one fetch of IWV gets you
        Russell 3000, and the small follow-up fetches of IVV (~500 tickers)
        and IWB (~1000 tickers) get you S&P 500 and Russell 1000 free.
        IWM doesn't need its own fetch — it's IWV minus IWB.

        Returns True if at least IWV was fetched successfully (the only
        source we strictly require).
        """
        def _log(m):
            if log_fn is not None:
                try: log_fn(m)
                except Exception: pass

        # v4.10.1: always fetch IWV first. It's the superset; everything
        # else filters from it.
        iwv_ok = self._fetch_one('iwv', log_fn=_log)

        # Then fetch IVV and IWB for membership tagging. Failures here
        # are non-fatal — IWV alone still works for scanning, just no
        # subset filtering until these succeed on a later refresh.
        ivv_ok = self._fetch_one('ivv', log_fn=_log)
        iwb_ok = self._fetch_one('iwb', log_fn=_log)

        if not iwv_ok and not (ivv_ok or iwb_ok):
            # Total fetch failure. _fetch_one will have set fallback for IWV.
            return False

        # Log a summary of what we ended up with
        try:
            iwv_n = len(self._caches.get('iwv', {}).get('tickers', []))
            ivv_n = len(self._caches.get('ivv', {}).get('tickers', []))
            iwb_n = len(self._caches.get('iwb', {}).get('tickers', []))
            iwm_n = max(0, iwv_n - iwb_n)
            _log(f"Universe ready: IWV={iwv_n}, IWB={iwb_n}, "
                 f"IVV={ivv_n}, IWM={iwm_n} (computed)")
        except Exception:
            pass

        return iwv_ok

    # v4.14.5.73-sec-universe-importer: SEC source.
    _SEC_UNIVERSE_URL = (
        "https://www.sec.gov/files/company_tickers_exchange.json")
    _SEC_UNIVERSE_MIN_EXPECTED = 4000
    _SEC_UNIVERSE_UA = "TiredMarket/4.14 admin@tiredmarket.local"

    # v4.14.5.74-drop-preferred-shares: preferred-share suffix filter.
    # The SEC returns ~1,000 preferred-share tickers using the dash-P
    # convention: `-PA`, `-PB`, `-PC`, `-PD`, `-PE`, `-PJ`, `-PR`, etc.
    # These are fixed-income-like instruments, not common-stock pick
    # candidates, and they flood the fundamentals/earnings logs with
    # "no data" misses. Drop them at source.
    #
    # CRITICAL: this MUST NOT drop legitimate dash-class common stock
    # — `BRK-B`, `BF-A`, `BF-B`, `MOG-A`, `LEN-B`, `HEI-A`, etc.
    # Those are dash + a non-`P` letter. The regex specifically anchors
    # on `-P` followed by zero or more additional letters at end of
    # ticker, so `BRK-B` and `BF-A` don't match.
    #
    # We deliberately do NOT try to filter the appended-5th-letter
    # preferred convention (e.g. AGNCL/AGNCM/AGNCN/AGNCO/AGNCP — AGNC
    # preferreds). A 5-letter ticker ending in a letter is usually a
    # normal Nasdaq ticker (GOOGL, CSCO, INTC) — too risky. Those few
    # residuals will simply tombstone as no-data, harmless.
    _PREFERRED_SHARE_SUFFIX_RE = re.compile(r'-P[A-Z]*$')

    @staticmethod
    def _fetch_universe_from_sec(
            log_fn: Callable[[str], None] | None = None
    ) -> list[str]:
        """v4.14.5.73-sec-universe-importer: fetch the canonical US-listed
        common-stock universe from the SEC's
        company_tickers_exchange.json endpoint.

        Format: {"fields":["cik","name","ticker","exchange"],
                 "data":[[cik, name, ticker, exchange], ...]}.

        Keeps rows where exchange == 'NYSE' or 'Nasdaq'. OTC (illiquid
        ADR junk), null-exchange, and CBOE (~27 names) rows are dropped.
        Tickers already in `BRK-B` dash form, just uppercased + regex-
        filtered against the same shape the legacy iShares parser used.

        Returns [] on any fetch/parse failure — caller handles fallback.
        """
        def _log(m):
            if log_fn is not None:
                try: log_fn(m)
                except Exception: pass

        try:
            import urllib.request
            req = urllib.request.Request(
                Universe._SEC_UNIVERSE_URL,
                headers={'User-Agent': Universe._SEC_UNIVERSE_UA,
                         'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode('utf-8', errors='ignore')
            data = json.loads(raw)
        except Exception as e:
            _log(f"SEC universe fetch failed: {e}")
            return []

        fields = data.get('fields') or []
        rows = data.get('data') or []
        if not rows or not fields:
            _log("SEC universe fetch returned no rows/fields.")
            return []

        try:
            t_idx = fields.index('ticker')
            x_idx = fields.index('exchange')
        except ValueError:
            _log(f"SEC universe: unexpected fields header {fields!r}")
            return []

        kept_exchanges = {'NYSE', 'Nasdaq'}
        seen: set[str] = set()
        out: list[str] = []
        n_total = len(rows)
        n_otc = n_null = n_cboe = n_other = n_preferred = 0
        for row in rows:
            try:
                tk = (row[t_idx] or '').strip().upper()
                ex = row[x_idx]
            except Exception:
                continue
            if not tk:
                continue
            if ex is None:
                n_null += 1; continue
            ex_str = str(ex).strip()
            if ex_str == 'OTC':
                n_otc += 1; continue
            if ex_str == 'CBOE':
                n_cboe += 1; continue
            if ex_str not in kept_exchanges:
                n_other += 1; continue
            if not re.match(r'^[A-Z][A-Z0-9.\-]{0,9}$', tk):
                continue
            # v4.14.5.74-drop-preferred-shares: drop -P* preferred shares
            # AFTER the shape regex (so logged count reflects only well-
            # formed preferreds) and BEFORE dedup (so we don't waste a
            # set slot on a name we'll discard).
            if Universe._PREFERRED_SHARE_SUFFIX_RE.search(tk):
                n_preferred += 1; continue
            if tk in seen:
                continue
            seen.add(tk)
            out.append(tk)

        _log(f"SEC universe: {n_total} rows -> {len(out)} kept "
             f"(dropped {n_otc} OTC / {n_null} null / "
             f"{n_cboe} CBOE / {n_preferred} preferred / "
             f"{n_other} other)")
        return out

    def _bundled_snapshot_tickers(self) -> list[str]:
        """v4.14.5.73-sec-universe-importer: last-resort fallback list.

        Tries to load the bundled clean-build snapshot of universe_iwv.json
        before falling back to FALLBACK_UNIVERSE (the small 113-name
        curated list). Used only when SEC fetch fails AND no usable
        on-disk cache exists.
        """
        candidates = [
            Path(__file__).parent / "_cleanbuild_staging" / "data"
                / "universe_iwv.json",
            # v4.14.6.108-standalone-prep: was "universe_iwv.bundled.json" — the
            # bundled asset is named universe_iwv.json, so the frozen lookup
            # missed it and fell back to the 113-ticker FALLBACK_UNIVERSE.
            # Match the real bundled filename so frozen seeds the full universe.
            __import__('tm_paths').get_app_asset_dir() / "universe_iwv.json",
        ]
        for p in candidates:
            try:
                if p.exists():
                    with open(p, 'r', encoding='utf-8') as f:
                        d = json.load(f)
                    tk = list(d.get('tickers') or [])
                    if len(tk) >= 500:
                        return tk
            except Exception:
                continue
        return list(FALLBACK_UNIVERSE)

    def _fetch_one(self, source_key: str,
                    log_fn: Callable[[str], None] | None = None) -> bool:
        """v4.10.1: Fetch a single source's ticker list and cache it.

        v4.14.5.73-sec-universe-importer: IWV now refreshes from the SEC's
        company_tickers_exchange.json endpoint (the iShares CSV URL was
        Cloudflare-blocked and returning 0 tickers every cycle). IVV /
        IWB / ITOT iShares fetches are NO-OP'd — they were also blocked
        and were spamming the log every refresh tick. Their cached
        membership lists stay on disk (frozen-membership snapshot, same
        as before, just no longer hammering a dead URL). Downstream
        readers (Universe.membership_for, sources_for) continue to work
        against the cached lists.

        Returns True on real-data success, False on fallback or failure.
        """
        def _log(m):
            if log_fn is not None:
                try: log_fn(m)
                except Exception: pass

        source_info = self.SOURCES.get(source_key, {})
        url = source_info.get('url')
        label = source_info.get('label', source_key)

        # Skip sources that are computed from membership (IWM)
        if source_info.get('computed_from_membership'):
            return True  # nothing to fetch — it's derived

        # v4.15.0 Step 22a: hardcoded ticker lists (DOW, NASDAQ-100).
        hardcoded = source_info.get('hardcoded_tickers')
        if hardcoded:
            tickers = list(hardcoded)
            with self._lock:
                self._caches[source_key] = {
                    "tickers": tickers,
                    "fetched_at": datetime.now().isoformat(),
                    "source": f"{source_key}_hardcoded",
                }
            self._save_one(source_key)
            _log(f"{label}: {len(tickers)} hardcoded tickers loaded.")
            return True

        # v4.14.5.73-sec-universe-importer: IWV is sourced from the SEC.
        if source_key == 'iwv':
            tickers = self._fetch_universe_from_sec(log_fn=_log)
            if len(tickers) >= self._SEC_UNIVERSE_MIN_EXPECTED:
                _log(f"Fetched {len(tickers)} tickers from SEC "
                     f"(NYSE+Nasdaq common stock).")
                with self._lock:
                    self._caches[source_key] = {
                        "tickers": tickers,
                        "fetched_at": datetime.now().isoformat(),
                        "source": "sec_company_tickers_exchange_"
                                  "nyse_nasdaq",
                    }
                self._save_one(source_key)
                return True
            # Short / empty SEC fetch — DO NOT overwrite a good cache.
            _log(f"SEC universe fetch returned only {len(tickers)} "
                 f"tickers (need >= {self._SEC_UNIVERSE_MIN_EXPECTED}).")
            cache = self._caches.get(source_key, {})
            if cache.get("tickers"):
                _log(f"  Keeping previously-cached {label} list "
                     f"({len(cache['tickers'])} tickers).")
                return False
            # Truly empty cache — bundled snapshot first, FALLBACK_UNIVERSE
            # only if even that's missing.
            fb = self._bundled_snapshot_tickers()
            _log(f"  Using bundled-snapshot fallback for {label} "
                 f"({len(fb)} tickers).")
            with self._lock:
                self._caches[source_key] = {
                    "tickers": list(fb),
                    "fetched_at": datetime.now().isoformat(),
                    "source": ("bundled_snapshot" if len(fb) >= 500
                               else "fallback"),
                }
            self._save_one(source_key)
            return False

        # v4.14.5.73-sec-universe-importer: iShares per-index URLs are
        # Cloudflare-blocked. Stop calling them on auto-refresh; keep
        # the cached membership snapshot on disk so membership_for and
        # sources_for continue to work against frozen membership data.
        if source_key in ('ivv', 'iwb', 'itot'):
            cache = self._caches.get(source_key, {})
            n_cached = len(cache.get('tickers') or [])
            if n_cached:
                _log(f"{label}: skipping refresh (iShares URL Cloudflare-"
                     f"blocked); keeping cached membership "
                     f"({n_cached} tickers).")
                return True
            _log(f"{label}: no cached membership and iShares URL "
                 f"Cloudflare-blocked — subset filtering will be coarse.")
            return False

        if not url:
            _log(f"{label}: no fetch URL configured. Skipping.")
            return False

        try:
            import urllib.request
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 Chrome/124.0 Safari/537.36'
            })
            with urllib.request.urlopen(req, timeout=20) as r:
                content = r.read().decode('utf-8', errors='ignore')
            tickers = self._parse_iwm_csv(content)
            min_expected = {'ivv': 400, 'iwb': 800, 'iwm': 1500,
                             'iwv': 2500}.get(source_key, 50)
            if len(tickers) < min_expected:
                raise ValueError(
                    f"only got {len(tickers)} tickers from {label} "
                    f"(expected at least {min_expected})")
            _log(f"Fetched {len(tickers)} tickers from {label}.")
            with self._lock:
                self._caches[source_key] = {
                    "tickers": tickers,
                    "fetched_at": datetime.now().isoformat(),
                    "source": f"{source_key}_holdings",
                }
            self._save_one(source_key)
            return True
        except Exception as e:
            _log(f"{label} fetch failed: {e}")
            cache = self._caches.get(source_key, {})
            if cache.get("tickers"):
                _log(f"  Keeping previously-cached {label} list.")
                return False
            return False

    @staticmethod
    def _parse_iwm_csv(content: str) -> list[str]:
        """Parse iShares CSV. Format works for IVV/IWB/IWM/etc."""
        import csv
        from io import StringIO
        tickers = []
        try:
            lines = content.splitlines()
            header_idx = None
            for i, line in enumerate(lines):
                if line.lstrip('"').startswith('Ticker'):
                    header_idx = i
                    break
            if header_idx is None:
                return []
            csv_text = '\n'.join(lines[header_idx:])
            reader = csv.DictReader(StringIO(csv_text))
            for row in reader:
                tk = (row.get('Ticker') or '').strip().upper()
                ac = (row.get('Asset Class') or '').strip().lower()
                if not tk or ac and ac not in ('equity', 'stock'):
                    continue
                if not re.match(r'^[A-Z][A-Z0-9.\-]{0,9}$', tk):
                    continue
                if tk in ('XTSLA', 'MARGIN_USD', 'USD', 'CASH', '-'):
                    continue
                tickers.append(tk)
        except Exception:
            return []
        seen = set()
        out = []
        for t in tickers:
            if t not in seen:
                seen.add(t); out.append(t)
        return out


# ─── v4.13.0: History-aware prefilter scoring ──────────────────────────
#
# Problem this solves: the prefilter passes ~75 candidates per scan based
# only on price/volume/news. The AI then evaluates each one. A consistent
# pattern emerged in the user's data: certain stocks (ERAS, OWL, SOUN, PTON,
# JBLU, LCID, WU, ...) keep getting AVOID'd by every model on every
# scan, but the prefilter has no memory and keeps feeding them.
# Meanwhile real winners (IONQ, ORKA, USAR, OKLO) get the same treatment
# as the consistent losers.
#
# Fix: after basic price/volume filtering, re-rank candidates using their
# AI history. Stocks with a high BUY-rate get promoted; stocks with a
# high AVOID-rate get demoted. The candidates list is the same, just
# ordered better — so the user still sees them all, but the most
# promising ones rise to the top of Discover's queue.
#
# Scoring is conservative: it only kicks in for tickers with N>=3
# predictions in the lookback window, and only counts predictions
# from the same path (slow_safe BUYs don't justify lottery promotion).
# Tickers with no history pass through neutral (score 0).

PREFILTER_HISTORY_WINDOW_DAYS = 7
PREFILTER_HISTORY_MIN_N = 3  # need at least 3 calls to have an opinion


def compute_history_scores(predictions_log, path,
                             window_days=PREFILTER_HISTORY_WINDOW_DAYS,
                             weight_lookup=None):
    """Compute a per-ticker history score from predictions.jsonl.

    Returns dict mapping ticker -> {
        'score': float in [-1, +1] where +1 = all BUY, -1 = all AVOID,
        'n_buy': int,
        'n_hold': int,
        'n_avoid': int,
        'n_total': int,
    }

    Tickers with fewer than PREFILTER_HISTORY_MIN_N predictions in the
    window are not included in the dict — they get neutral default
    treatment in the caller.

    `path` filters predictions to the same path. Cross-path BUYs don't
    count: a stock that aggressive likes might not be a lottery pick.
    Pass None to include all paths (mostly useful for testing).
    """
    if predictions_log is None:
        return {}

    try:
        all_preds = []
        if hasattr(predictions_log, 'get_all'):
            all_preds = predictions_log.get_all_full(timeout=30.0) or []
        elif hasattr(predictions_log, 'read_all'):
            all_preds = predictions_log.read_all() or []
        else:
            return {}
    except Exception:
        return {}

    # Filter to path + recency
    cutoff_ts = (datetime.now() - timedelta(days=window_days)).isoformat()
    filtered = []
    for p in all_preds:
        if path is not None and p.get('path') != path:
            continue
        ts = p.get('timestamp', '')
        if ts and ts < cutoff_ts:
            continue
        filtered.append(p)

    # Aggregate by ticker
    from collections import defaultdict
    # v4.14.2 stage 4: WATCH joins HOLD as a non-buy / non-avoid
    # third option. Both bin into the same "neither promote nor
    # demote" semantic — score uses n_buy - n_avoid so WATCH /
    # HOLD don't push the score in either direction.
    # v4.14.5.19-accuracy-weighted-consensus: when weight_lookup is
    # provided (caller passed a {model -> weight in [1,9]} resolver),
    # each prediction's contribution to the score numerator is scaled
    # by weight/NEUTRAL_WEIGHT (so a neutral n=0/thin model contributes
    # exactly 1.0 unit -- score identical to flat tally for cold-start
    # users -- and mature high-accuracy models contribute more,
    # mature low-accuracy models contribute less). Raw counts stay
    # un-weighted so the PREFILTER_HISTORY_MIN_N gate and reported
    # n_buy/n_hold/n_avoid/n_total reflect actual prediction counts.
    counts = defaultdict(lambda: {'n_buy': 0, 'n_hold': 0,
                                    'n_avoid': 0, 'n_watch': 0,
                                    'n_total': 0,
                                    'w_buy': 0.0, 'w_avoid': 0.0,
                                    'w_total': 0.0})
    _NEUTRAL = 5.0  # mirrors tm_source_accuracy.NEUTRAL_WEIGHT
    for p in filtered:
        ticker = (p.get('ticker') or '').upper()
        if not ticker:
            continue
        direction = (p.get('direction') or '').upper().strip()
        c = counts[ticker]
        c['n_total'] += 1
        # Per-prediction weight (1.0 == neutral / cold-start equivalent).
        if weight_lookup is not None:
            try:
                w = float(weight_lookup(p.get('model') or '')) / _NEUTRAL
            except Exception:
                w = 1.0
        else:
            w = 1.0
        c['w_total'] += w
        if direction == 'BUY':
            c['n_buy'] += 1
            c['w_buy'] += w
        elif direction == 'HOLD':
            c['n_hold'] += 1
        elif direction == 'AVOID':
            c['n_avoid'] += 1
            c['w_avoid'] += w
        elif direction == 'WATCH':       # v4.14.2 stage 4
            c['n_watch'] += 1

    # Compute scores, drop tickers with too-small N
    out = {}
    for ticker, c in counts.items():
        if c['n_total'] < PREFILTER_HISTORY_MIN_N:
            continue
        # Score: (w_buy - w_avoid) / w_total -- when weight_lookup is
        # None, w_* == n_* so score is byte-identical to the prior
        # (n_buy - n_avoid) / n_total formula. With weight_lookup set,
        # mature models tilt the score relative to neutral.
        wtotal = c['w_total'] or c['n_total']
        score = (c['w_buy'] - c['w_avoid']) / wtotal if wtotal else 0.0
        out[ticker] = {
            'score': score,
            'n_buy': c['n_buy'],
            'n_hold': c['n_hold'],
            'n_avoid': c['n_avoid'],
            'n_total': c['n_total'],
        }
    return out


def apply_history_scores(candidates, history_scores, enabled=True):
    """Re-order candidates using history scores.

    The base liquidity sort is preserved as a tie-breaker; we just bias
    the order using the history-derived score. Tickers with no history
    (not in history_scores) get neutral position — between the BUY-tilted
    and AVOID-tilted ones.

    enabled=False is a pass-through. Lets the caller toggle the feature
    on/off via config without a separate code path.

    Each candidate dict is annotated with 'history_score' and 'history_n'
    fields so callers (Settings, log) can display them.
    """
    # Annotate every candidate, even ones with no history
    for c in candidates:
        ticker = c.get('ticker', '').upper()
        h = history_scores.get(ticker)
        if h:
            c['history_score'] = h['score']
            c['history_n_buy'] = h['n_buy']
            c['history_n_avoid'] = h['n_avoid']
            c['history_n'] = h['n_total']
        else:
            c['history_score'] = 0.0
            c['history_n'] = 0

    if not enabled:
        return candidates

    # Sort by history_score DESC (BUY-heavy first), then by liquidity DESC
    # as tie-breaker.
    candidates.sort(
        key=lambda c: (
            c.get('history_score', 0.0),
            (c.get('price', 0) or 0) * (c.get('volume', 0) or 0),
        ),
        reverse=True,
    )
    return candidates


# ─── Pre-filter for universe scanning ──────────────────────────────────

# Per-path filter parameters. The pre-filter narrows the full universe
# down to a manageable list BEFORE the AI is asked to analyze anything.
# Path-aware: slow_safe wants real companies, lottery wants speculation.
PATH_FILTER_PARAMS = {
    'slow_safe': {
        'min_price': 5.0,
        'max_price': 1000.0,
        'min_avg_volume': 500_000,
        'max_drop_30d_pct': 30.0,   # don't catch falling knives
        'min_news_count_7d': 1,
    },
    'moderate': {
        'min_price': 2.0,
        'max_price': 500.0,
        'min_avg_volume': 200_000,
        'max_drop_30d_pct': 40.0,
        'min_news_count_7d': 1,
    },
    'aggressive': {
        'min_price': 0.50,
        'max_price': 100.0,
        'min_avg_volume': 100_000,
        'max_drop_30d_pct': 60.0,
        'min_news_count_7d': 0,
    },
    'lottery': {
        'min_price': 1.00,           # v4.13.0+: $1-$10 microcap range.
                                     # Below $1 = use 'penny_lottery' path.
        'max_price': 10.0,
        'min_avg_volume': 50_000,
        'max_drop_30d_pct': 80.0,   # lottery accepts ugly charts
        'min_news_count_7d': 0,
    },
    'penny_lottery': {
        'min_price': 0.10,           # v4.13.1: true sub-dollar long shots.
        'max_price': 2.00,           # overlap with lottery is intentional;
                                     # gives the user a way to surface sub-$1
                                     # plays explicitly when he wants them.
        'min_avg_volume': 25_000,    # lower than lottery — penny names are thin
        'max_drop_30d_pct': 90.0,    # accepts the ugliest charts
        'min_news_count_7d': 0,
    },
}


# ─── Pre-filter result cache ──────────────────────────────────────────

# Cache of filter_candidates results, keyed by (source, path, date).
# Avoids re-running the expensive Yahoo batch fetch when the user clicks
# Run Discovery twice in a row (e.g., to retry after closing the panel).
# v4.8.11: now persisted to data/prefilter_cache.json so app restart no
# longer wipes the cache (which previously caused yfinance rate-limit
# spirals — every restart = full quote refetch).
# Cache invalidates after PREFILTER_CACHE_TTL_SECONDS.
#
# v4.13.53: bumped from 4 hours to 7 days. The pre-filter only uses this
# cache for ELIGIBILITY questions ("is this stock between $5 and $1000,
# does it trade enough volume, etc.") — answers that are stable for
# days/weeks. The 4-hour TTL was forcing daily full-universe re-fetches
# from Yahoo, which is what kept tripping the rate-limit cooldowns. The
# AI scoring phase still uses fresh prices via the separate quote_cache,
# so this change doesn't affect prediction accuracy — it only stops
# us from re-fetching 2,500+ universe quotes every morning.
PREFILTER_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days

_prefilter_cache: dict[tuple, tuple[float, list]] = {}
_prefilter_cache_lock = threading.Lock()
_prefilter_cache_path: 'Path | None' = None  # set by set_prefilter_cache_path
_prefilter_cache_loaded = False  # one-time load guard

# Hooks for cache-hit/miss diagnostic logging. Caller (main app) sets
# _prefilter_log_fn to a function(msg: str) and we call it on hit/miss.
# Default is a no-op so the module stays importable standalone.
_prefilter_log_fn = None


def set_prefilter_cache_path(path):
    """Tell the cache where to persist itself (called once at app init).
    Pass a Path or None. None disables persistence (cache stays in-memory)."""
    global _prefilter_cache_path
    _prefilter_cache_path = path


def set_prefilter_log_fn(fn):
    """Register a logging callback for cache hit/miss diagnostics.
    fn takes a single string argument. Pass None to disable."""
    global _prefilter_log_fn
    _prefilter_log_fn = fn


def _prefilter_log(msg: str):
    fn = _prefilter_log_fn
    if fn is None:
        return
    try: fn(msg)
    except Exception: pass


def _prefilter_cache_load_from_disk():
    """Load persisted cache from disk on first access. Best-effort:
    any error (missing file, corruption, schema drift) leaves the cache
    empty and is logged but not raised. Cache keys are stored as
    JSON arrays (since tuples don't survive JSON), reconstituted to
    tuples on load."""
    global _prefilter_cache_loaded
    if _prefilter_cache_loaded:
        return
    _prefilter_cache_loaded = True  # set first so failures don't loop
    p = _prefilter_cache_path
    if p is None:
        return
    if not p.exists():
        # v4.8.12: surface this as a startup log line so user knows
        # persistence is wired but no cache file exists yet.
        _prefilter_log(
            "Pre-filter cache: no cache file on disk yet "
            "(will be created after next scan)")
        return
    try:
        import json as _json
        raw = p.read_text(encoding='utf-8')
        data = _json.loads(raw)
        if not isinstance(data, list):
            return
        loaded = 0
        skipped_stale = 0
        now = time.time()
        for entry in data:
            try:
                key = tuple(entry['key'])
                cached_at = float(entry['cached_at'])
                results = list(entry['results'])
                if now - cached_at > PREFILTER_CACHE_TTL_SECONDS:
                    skipped_stale += 1
                    continue
                _prefilter_cache[key] = (cached_at, results)
                loaded += 1
            except Exception:
                continue  # skip bad entries, keep good ones
        if loaded or skipped_stale:
            _prefilter_log(
                f"Pre-filter cache: loaded {loaded} entry(s) from disk"
                + (f", skipped {skipped_stale} stale" if skipped_stale else ""))
        else:
            # v4.8.12: always log a line so user can confirm persistence is alive
            _prefilter_log(
                "Pre-filter cache: 0 entries on disk (will populate after next scan)")
    except Exception as e:
        _prefilter_log(f"Pre-filter cache: could not load from disk ({e})")


def _prefilter_cache_save_to_disk():
    """Persist current cache to disk. Best-effort, errors are logged
    but do not propagate. Atomic write via temp file + rename so an
    interrupted save doesn't corrupt the cache file."""
    p = _prefilter_cache_path
    if p is None:
        return
    try:
        import json as _json
        with _prefilter_cache_lock:
            snapshot = [
                {
                    'key': list(key),
                    'cached_at': cached_at,
                    'results': results,
                }
                for key, (cached_at, results) in _prefilter_cache.items()
            ]
        tmp = p.with_suffix(p.suffix + '.tmp')
        tmp.write_text(_json.dumps(snapshot), encoding='utf-8')
        tmp.replace(p)
    except Exception as e:
        _prefilter_log(f"Pre-filter cache: could not save to disk ({e})")


def _prefilter_cache_get(key: tuple) -> list | None:
    _prefilter_cache_load_from_disk()
    with _prefilter_cache_lock:
        entry = _prefilter_cache.get(key)
        if not entry:
            _prefilter_log(
                f"Pre-filter cache MISS for key={key} — will fetch quotes")
            return None
        cached_at, results = entry
        age_sec = time.time() - cached_at
        if age_sec > PREFILTER_CACHE_TTL_SECONDS:
            _prefilter_cache.pop(key, None)
            _prefilter_log(
                f"Pre-filter cache EXPIRED for key={key} "
                f"(age {age_sec/60:.0f}min > TTL {PREFILTER_CACHE_TTL_SECONDS/60:.0f}min)")
            return None
        age_min = int(age_sec / 60)
        _prefilter_log(
            f"Pre-filter cache HIT for key={key} "
            f"(data age {age_min} min, {len(results)} candidate(s))")
        return list(results)


def _prefilter_cache_put(key: tuple, results: list):
    with _prefilter_cache_lock:
        _prefilter_cache[key] = (time.time(), list(results))
    # Persist after every put. Cheap (small JSON), avoids losing data
    # on unexpected exit. Called outside the lock to avoid contention.
    _prefilter_cache_save_to_disk()


def recently_analyzed_tickers(predictions_log,
                                hours: int = 24) -> set[str]:
    """Return set of tickers that have been analyzed in the last N hours.
    Used to skip re-analyzing the same ticker too frequently — saves time
    and keeps prediction log from filling with redundant entries.

    Caller can decide whether to filter these from the AI scoring step
    (saves time) or include them anyway (gets fresh take).
    """
    if predictions_log is None:
        return set()
    cutoff = datetime.now() - timedelta(hours=hours)
    out = set()
    try:
        for p in predictions_log.get_all_full(timeout=30.0):
            try:
                ts = datetime.fromisoformat(p.get('timestamp', ''))
                if ts >= cutoff:
                    tk = p.get('ticker', '').upper()
                    if tk:
                        out.add(tk)
            except Exception:
                continue
    except Exception:
        pass
    return out


# ════════════════════════════════════════════════════════════════════════
# v4.8.13 — CONSENSUS COMPUTATION
# ════════════════════════════════════════════════════════════════════════
#
# Groups recent predictions by ticker and scores how strongly multiple AI
# models agree. The classification levels are deliberately simple — direction
# agreement first, confidence quality as a secondary signal.
#
# Sort key (consensus_score, descending) is roughly:
#   3.0 = unanimous strong (all 3+ models BUY/AVOID with MOD or HIGH conf)
#   2.5 = unanimous weak (all agree direction, mixed confidence)
#   2.0 = majority strong (2 of 3 agree at MOD+, 1 dissents)
#   1.5 = majority weak (2 of 3 agree, mixed confidence)
#   1.0 = single-model only (only one model has analyzed this ticker)
#   0.0 = full disagreement (every model said something different)


def compute_per_model_stats(predictions_log,
                              hours: int = 168) -> list[dict]:
    """v4.9.1: Aggregate per-model statistics for the Track Record stats card.

    Args:
        predictions_log: PredictionsLog instance
        hours: only include predictions from last N hours (default 168 = 7 days)

    Returns one entry per model, sorted by total predictions (most active first):
        {
            'model': str,
            'total': int,
            'directions': {'BUY': N, 'AVOID': N, 'HOLD': N, 'NO_CALL': N},
            'confidences': {'MOD': N, 'LOW': N, 'NONE': N, ...},
            'closed': int,         # how many predictions resolved
            'target_hits': int,
            'stop_hits': int,
            'expired': int,
            'open': int,
            'target_hit_rate_pct': float,  # only meaningful with 5+ closed
            'stop_hit_rate_pct': float,
            'has_meaningful_outcomes': bool,  # True only if 5+ closed
        }
    """
    if predictions_log is None:
        return []
    # v4.14.6.35-fix-startup-stampede: working-set sufficient. This
    # path is time-windowed to `hours` (default 168 = 7 days). The
    # working set (recent 2000 records, easily covering weeks of
    # ingest) is more than enough; the v4.14.6.34 audit
    # miscategorised this as needing full history. Reverting from
    # get_all_full so consensus-runner first-tick doesn't block on
    # the predictions tail-load.
    try:
        all_preds = predictions_log.get_all()
    except Exception:
        return []

    cutoff = datetime.now() - timedelta(hours=hours)

    # v4.14.5.62-validated-accuracy: shared attribution gate (no-op when the
    # use_validated_accuracy flag is off → byte-identical to pre-patch).
    try:
        import tm_source_accuracy as _tsa_attr
    except Exception:
        _tsa_attr = None

    # Group by model
    by_model: dict[str, list[dict]] = {}
    for p in all_preds:
        try:
            ts = datetime.fromisoformat(p.get('timestamp', ''))
        except Exception:
            continue
        if ts < cutoff:
            continue
        if _tsa_attr is not None and not _tsa_attr.is_attributable(p):
            continue
        model = (p.get('model') or '').strip() or 'unknown'
        by_model.setdefault(model, []).append(p)

    out = []
    for model, preds in by_model.items():
        directions: dict[str, int] = {}
        confidences: dict[str, int] = {}
        closed = 0
        target_hits = 0
        stop_hits = 0
        expired = 0
        open_count = 0
        # v4.14.5.14-canonical-accuracy-definition: retraction counters
        superseded = 0
        contradicted = 0

        for p in preds:
            d = (p.get('direction') or '?').upper()
            directions[d] = directions.get(d, 0) + 1
            cf = (p.get('confidence') or '?').upper()
            confidences[cf] = confidences.get(cf, 0) + 1

            status = (p.get('status') or 'open').lower()
            if status == 'open':
                open_count += 1
            else:
                closed += 1
                if status == 'target_hit':
                    target_hits += 1
                elif status == 'stop_hit':
                    stop_hits += 1
                elif status == 'expired':
                    expired += 1
                elif status == 'superseded':
                    superseded += 1
                elif status == 'contradicted':
                    contradicted += 1

        # v4.14.5.14-canonical-accuracy-definition: rate denominator is
        # DECIDED only (target_hit + stop_hit); non-verdicts excluded.
        decided = target_hits + stop_hits
        target_rate = (target_hits / decided * 100) if decided else 0.0
        stop_rate = (stop_hits / decided * 100) if decided else 0.0
        retract_rate = ((superseded + contradicted) / closed * 100) if closed else 0.0

        out.append({
            'model': model,
            'total': len(preds),
            'directions': directions,
            'confidences': confidences,
            'closed': closed,
            'target_hits': target_hits,
            'stop_hits': stop_hits,
            'expired': expired,
            'open': open_count,
            # v4.14.5.14-canonical-accuracy-definition: rates are now
            # target/(target+stop). 'closed' stays as informational total.
            'target_hit_rate_pct': target_rate,
            'stop_hit_rate_pct': stop_rate,
            'decided': decided,                 # target_hits + stop_hits
            'superseded': superseded,
            'contradicted': contradicted,
            'retract_rate_pct': retract_rate,
            # 5+ DECIDED predictions before we trust the rates at all.
            # Below that, the noise dominates and showing percentages
            # misleads users into pattern-matching on randomness.
            'has_meaningful_outcomes': decided >= 5,
        })

    # Sort by total predictions, most active first
    out.sort(key=lambda e: -e['total'])
    return out


def list_consensus_scan_ids(predictions_log,
                              hours: int = 168) -> list[dict]:
    """v4.9.1: Find distinct consensus scan IDs in the predictions log,
    along with friendly metadata for the dropdown picker.

    Args:
        predictions_log: PredictionsLog instance
        hours: window to look in (default 168 = 7 days)

    Returns list sorted newest-first:
        {
            'scan_id': str,
            'started_at': datetime,
            'friendly_label': str,  # e.g., "9:05pm 4-model scan (45 preds)"
            'model_count': int,
            'pred_count': int,
            'models': [model_name, ...],
        }
    """
    if predictions_log is None:
        return []
    try:
        all_preds = predictions_log.get_all_full(timeout=30.0)
    except Exception:
        return []

    cutoff = datetime.now() - timedelta(hours=hours)

    by_scan: dict[str, list[dict]] = {}
    for p in all_preds:
        sid = p.get('scan_id')
        if not sid:
            continue
        try:
            ts = datetime.fromisoformat(p.get('timestamp', ''))
        except Exception:
            continue
        if ts < cutoff:
            continue
        by_scan.setdefault(sid, []).append(p)

    out = []
    for sid, preds in by_scan.items():
        # Earliest timestamp in this scan = scan start
        try:
            timestamps = [datetime.fromisoformat(p.get('timestamp', ''))
                          for p in preds]
            started_at = min(timestamps)
        except Exception:
            started_at = datetime.now()

        models = sorted(set(p.get('model', '') for p in preds
                            if p.get('model')))

        # Friendly label: "Apr 27 9:05pm  ·  4-model scan  ·  45 preds"
        # Use 12-hour with am/pm for readability
        try:
            time_str = started_at.strftime('%b %-d %-I:%M%p').lower()
        except (ValueError, TypeError):
            # Windows uses different format codes
            try:
                time_str = started_at.strftime('%b %#d %#I:%M%p').lower()
            except Exception:
                time_str = started_at.strftime('%Y-%m-%d %H:%M')
        friendly = (f"{time_str}  ·  {len(models)}-model scan  ·  "
                    f"{len(preds)} preds")

        out.append({
            'scan_id': sid,
            'started_at': started_at,
            'friendly_label': friendly,
            'model_count': len(models),
            'pred_count': len(preds),
            'models': models,
        })

    out.sort(key=lambda e: e['started_at'], reverse=True)
    return out


def compute_consensus(predictions_log,
                        hours: int = 48,
                        min_models: int = 2,
                        scan_id: str | None = None) -> list[dict]:
    """Return a list of consensus entries, one per ticker, sorted with
    strongest consensus first.

    Args:
        predictions_log: PredictionsLog instance
        hours: only consider predictions made in the last N hours
            (default 48 — long enough to span an A/B test run, short
            enough that prices haven't moved much).
        min_models: only include tickers that have predictions from at
            least this many distinct models (default 2; set to 1 to see
            single-model entries too).
        scan_id: v4.9.1 — if provided, only include predictions tagged
            with this exact scan_id (i.e., from one specific consensus
            run). When None (default), all recent predictions are
            included regardless of scan_id.

    Each entry:
        {
            'ticker': str,
            'predictions': [pred_dict, ...],  # most recent per model
            'model_count': int,
            'directions': {'BUY': 2, 'AVOID': 1},  # counts
            'majority_direction': 'BUY' or None,
            'majority_count': int,
            'agreement_pct': float (0-100),
            'consensus_score': float (0-3),
            'consensus_label': str,  # human-readable
            'consensus_color': str,  # 'green' / 'amber' / 'red' / 'muted'
            'confidence_summary': str,  # e.g., "MOD/MOD/LOW"
        }
    """
    if predictions_log is None:
        return []
    # v4.14.6.35-fix-startup-stampede: working-set sufficient. This
    # consensus path is time-windowed to `hours` (default 48 = 2 days)
    # and only considers the most-recent prediction per (ticker,
    # model) inside that window. The working set covers it.
    try:
        all_preds = predictions_log.get_all()
    except Exception:
        return []

    cutoff = datetime.now() - timedelta(hours=hours)

    # Group by (ticker, model) — keep only most recent per pair within window
    # Then collapse to per-ticker dict: {ticker: {model: pred}}
    by_ticker_model: dict[str, dict[str, dict]] = {}
    for p in all_preds:
        try:
            ts = datetime.fromisoformat(p.get('timestamp', ''))
        except Exception:
            continue
        if ts < cutoff:
            continue
        # v4.9.1: scan_id filter — only include preds with matching ID
        if scan_id is not None and p.get('scan_id') != scan_id:
            continue
        ticker = (p.get('ticker') or '').upper()
        model = (p.get('model') or '').strip() or 'unknown'
        if not ticker:
            continue
        bucket = by_ticker_model.setdefault(ticker, {})
        prev = bucket.get(model)
        if prev is None:
            bucket[model] = p
        else:
            try:
                prev_ts = datetime.fromisoformat(prev.get('timestamp', ''))
                if ts > prev_ts:
                    bucket[model] = p
            except Exception:
                bucket[model] = p

    out = []
    for ticker, model_preds in by_ticker_model.items():
        if len(model_preds) < min_models:
            continue
        preds = list(model_preds.values())

        # v4.8.14: separate NO_CALL predictions from directional ones.
        # NO_CALL means a model was attempted but declined to give a call
        # (typical for phi4 on ambiguous setups). It shouldn't count as
        # agreeing with any direction, but it's important to surface so
        # the user knows the model was actually tried.
        no_call_count = sum(
            1 for p in preds
            if (p.get('direction') or '').upper() == 'NO_CALL')
        directional_preds = [
            p for p in preds
            if (p.get('direction') or '').upper() != 'NO_CALL']

        # Tally directions (only directional predictions)
        dir_counts: dict[str, int] = {}
        for p in directional_preds:
            d = (p.get('direction') or '').upper() or 'NONE'
            dir_counts[d] = dir_counts.get(d, 0) + 1

        majority_dir = None
        majority_count = 0
        for d, c in dir_counts.items():
            if c > majority_count:
                majority_dir = d
                majority_count = c
            elif c == majority_count:
                # Tie — no single majority
                majority_dir = None

        # Agreement % is over directional predictions only
        directional_count = len(directional_preds)
        agreement_pct = ((majority_count / directional_count * 100)
                         if directional_count else 0.0)

        # Confidence summary — sorted to make it stable. Include all preds
        # (including NO_CALL with NONE confidence) so user sees full picture.
        confs = [(p.get('confidence') or '?').upper()[:3] for p in preds]
        confidence_summary = '/'.join(sorted(confs, reverse=True))

        # Score & label — based on directional preds only
        # MOD/HIGH treated as "strong"; LOW treated as "weak"
        strong_count = sum(
            1 for p in directional_preds
            if (p.get('confidence') or '').upper() in ('MOD', 'MODERATE',
                                                        'HIGH'))
        all_strong = (directional_count > 0
                       and strong_count == directional_count)
        all_dir_agree = (majority_dir is not None
                          and majority_count == directional_count
                          and directional_count > 0)
        majority_dir_agree = (majority_dir is not None
                                and majority_count >= 2
                                and majority_count >
                                    directional_count - majority_count)

        # NO_CALL suffix added to all labels when relevant
        no_call_suffix = (f", {no_call_count} no-call"
                          if no_call_count else "")

        if directional_count == 0:
            # All models declined to give a call — interesting signal
            score = 0.5
            label = f"All {len(preds)} models declined (no-call)"
            color = 'muted'
        elif len(preds) == 1:
            # Single-model only
            score = 1.0
            label = "Single model"
            color = 'muted'
        elif all_dir_agree and directional_count < 2:
            # v4.13.5: Only 1 model voted directionally (others declined).
            # The old code labeled this 'Unanimous {dir}' which was
            # misleading — e.g. 1 BUY + 1 NO_CALL = 'Unanimous BUY'.
            # Honest label, lower score so it doesn't crowd real signals.
            score = 1.0
            label = (f"Single-voice {majority_dir} "
                     f"({len(preds) - directional_count} declined)")
            color = 'muted'
        elif all_dir_agree and all_strong:
            score = 3.0
            label = f"Unanimous {majority_dir} (strong){no_call_suffix}"
            color = ('green' if majority_dir == 'BUY'
                     else 'red' if majority_dir == 'AVOID'
                     else 'amber')
        elif all_dir_agree:
            score = 2.5
            label = (f"Unanimous {majority_dir} "
                     f"(mixed conf){no_call_suffix}")
            color = ('green' if majority_dir == 'BUY'
                     else 'red' if majority_dir == 'AVOID'
                     else 'amber')
        elif majority_dir_agree and strong_count >= majority_count:
            score = 2.0
            label = (f"Majority {majority_dir} "
                     f"({majority_count}/{directional_count}, "
                     f"strong){no_call_suffix}")
            color = ('green' if majority_dir == 'BUY'
                     else 'red' if majority_dir == 'AVOID'
                     else 'amber')
        elif majority_dir_agree:
            score = 1.5
            label = (f"Majority {majority_dir} "
                     f"({majority_count}/{directional_count}, "
                     f"weak){no_call_suffix}")
            color = 'amber'
        else:
            # Full disagreement
            score = 0.0
            dir_str = ', '.join(f"{d}:{c}"
                                for d, c in sorted(dir_counts.items()))
            label = f"Split ({dir_str}){no_call_suffix}"
            color = 'muted'

        out.append({
            'ticker': ticker,
            'predictions': preds,
            'model_count': len(preds),
            'directions': dir_counts,
            'majority_direction': majority_dir,
            'majority_count': majority_count,
            'agreement_pct': agreement_pct,
            'consensus_score': score,
            'consensus_label': label,
            'consensus_color': color,
            'confidence_summary': confidence_summary,
            'no_call_count': no_call_count,  # v4.8.14
        })

    # Sort: strongest consensus first, ties broken by ticker alphabetical
    out.sort(key=lambda e: (-e['consensus_score'], e['ticker']))
    return out


def batch_fetch_quotes(tickers: list[str],
                        chunk_size: int = 50,
                        progress_fn: Callable[[int, int], None] | None = None,
                        cancel_fn: Callable[[], bool] | None = None,
                        ) -> dict[str, dict]:
    """Fetch quotes for many tickers in batches using yfinance.

    Falls back to per-ticker fetch if yfinance batching fails. Returns
    a dict: ticker -> {price, volume, change_pct, change_30d_pct}.

    Massively faster than per-ticker on large universes:
        - Per-ticker on 2000 tickers: ~60-90 seconds
        - Batched (50 at a time): ~10-15 seconds

    Network failures on individual chunks are tolerated; partial results
    are returned. Caller should accept that some tickers may be missing.
    """
    out: dict[str, dict] = {}
    total = len(tickers)
    if total == 0:
        return out

    try:
        import yfinance as yf
    except ImportError:
        return out

    for chunk_start in range(0, total, chunk_size):
        if cancel_fn is not None:
            try:
                if cancel_fn():
                    break
            except Exception:
                pass

        chunk = tickers[chunk_start:chunk_start + chunk_size]
        if progress_fn is not None:
            try:
                progress_fn(chunk_start, total)
            except Exception:
                pass

        try:
            # yfinance.Tickers can fetch many at once efficiently
            ticker_str = ' '.join(chunk)
            tickers_obj = yf.Tickers(ticker_str)
            # Get fast quote info for each
            for tk in chunk:
                try:
                    t_obj = tickers_obj.tickers.get(tk) or tickers_obj.tickers.get(tk.upper())
                    if t_obj is None:
                        continue
                    fast = t_obj.fast_info
                    last = fast.get('last_price') or fast.get('regularMarketPrice')
                    prev = fast.get('previous_close') or fast.get('regularMarketPreviousClose')
                    vol = fast.get('last_volume') or fast.get('regularMarketVolume') or 0
                    if not last or last <= 0:
                        continue
                    change_pct = ((last - prev) / prev * 100) if prev else 0
                    out[tk.upper()] = {
                        'price': float(last),
                        'volume': int(vol) if vol else 0,
                        'change_pct': float(change_pct),
                        # change_30d_pct skipped here; would require history
                        # call per ticker. Pre-filter skips this check.
                        'change_30d_pct': None,
                    }
                except Exception:
                    continue
        except Exception:
            # Chunk failed entirely — skip and continue
            continue

    if progress_fn is not None:
        try:
            progress_fn(total, total)
        except Exception:
            pass

    return out


def filter_candidates(tickers: list[str],
                       quote_fn: Callable[[str], dict | None],
                       news_count_fn: Callable[[str], int] | None,
                       path: str = 'moderate',
                       held_tickers: set[str] | None = None,
                       max_results: int = 75,
                       progress_fn: Callable[[int, int], None] | None = None,
                       cancel_fn: Callable[[], bool] | None = None,
                       cache_key: tuple | None = None,
                       use_batch: bool = True,
                       on_batch_quote: Callable[[str, dict], None] | None = None,
                       universe = None,  # v4.10.1: for snapshot membership tags
                       history_scores: dict[str, dict] | None = None,  # v4.13.0
                       history_scores_enabled: bool = True,  # v4.13.0
                       ) -> list[dict]:
    """Apply path-aware filters to narrow a universe down to AI-worthy
    candidates.

    If cache_key is provided, results are cached for PREFILTER_CACHE_TTL
    seconds — re-running with the same key returns cached results without
    re-fetching quotes.

    If use_batch is True (default), uses batch_fetch_quotes to get
    all prices in one go (~5-10x faster than per-ticker quote_fn).
    Falls back to quote_fn for any tickers the batch missed.

    v4.8.14: If on_batch_quote is provided, it gets called for every
    successful batch-fetched quote with (ticker, quote_dict). This lets
    the caller seed their own DataCacheLayer so the AI scoring phase
    finds quotes already cached and doesn't refetch them — saves 15+
    redundant yfinance calls per scan and keeps borderline rate-limit
    situations from tipping over.
    """
    # Cache check
    if cache_key is not None:
        cached = _prefilter_cache_get(cache_key)
        if cached is not None:
            # v4.8.14: even on cache hit, seed the caller's cache with the
            # cached quote data so the AI phase doesn't refetch. The
            # cached entries already contain price/volume/change_pct.
            if on_batch_quote is not None:
                for c in cached:
                    tk = c.get('ticker', '')
                    if tk:
                        try: on_batch_quote(tk, c)
                        except Exception: pass
            # v4.13.0: history scoring is applied AFTER cache so toggling
            # the feature in Settings takes effect immediately, not after
            # the 4hr cache TTL expires. Cache stores the pre-history
            # ordering; we re-rank here every time.
            if history_scores is not None:
                cached = apply_history_scores(
                    list(cached), history_scores,
                    enabled=history_scores_enabled)
            return cached
    params = PATH_FILTER_PARAMS.get(path, PATH_FILTER_PARAMS['moderate'])
    held = held_tickers or set()
    held = {t.upper() for t in held}

    # Pre-fetch quotes in batch if requested (much faster than per-ticker)
    batch_quotes: dict[str, dict] = {}
    if use_batch and len(tickers) > 50:
        # Skip already-held tickers from the batch fetch entirely
        to_fetch = [t for t in tickers if t.upper() not in held]
        try:
            # Wrap progress reporter to label this phase explicitly
            batch_progress = None
            if progress_fn is not None:
                def batch_progress(idx, tot):
                    try: progress_fn(idx, tot, phase='batch_fetch')
                    except TypeError:
                        # Fall back to old signature if caller didn't update
                        try: progress_fn(idx, tot)
                        except Exception: pass
                    except Exception: pass
            batch_quotes = batch_fetch_quotes(
                to_fetch,
                chunk_size=50,
                progress_fn=batch_progress,
                cancel_fn=cancel_fn,
            )
            # v4.8.14: seed caller's cache with every successful batch quote.
            # This is the key optimization — the AI phase will hit cached
            # data instead of making 15 fresh per-ticker yfinance calls.
            if on_batch_quote is not None:
                for tk, q in batch_quotes.items():
                    if q:
                        try: on_batch_quote(tk, q)
                        except Exception: pass
        except Exception:
            batch_quotes = {}

    candidates = []
    total = len(tickers)
    # Track WHY tickers got rejected — useful for explaining "0 candidates"
    rej = {
        'held': 0,
        'no_quote': 0,
        'price_too_low': 0,
        'price_too_high': 0,
        'volume_too_low': 0,
        'falling_knife': 0,
        'no_news': 0,
    }
    for i, tk in enumerate(tickers):
        if cancel_fn is not None:
            try:
                if cancel_fn():
                    break
            except Exception:
                pass

        # Progress reporting only matters if we didn't already report via batch
        if progress_fn is not None and not batch_quotes and i % 25 == 0:
            try: progress_fn(i, total, phase='filter_loop')
            except TypeError:
                try: progress_fn(i, total)
                except Exception: pass
            except Exception: pass

        tu = tk.upper()
        if tu in held:
            rej['held'] += 1
            continue

        # Try batch first, fall back to per-ticker quote_fn
        q = batch_quotes.get(tu)
        if not q:
            try:
                q = quote_fn(tu)
            except Exception:
                q = None
        if not q:
            rej['no_quote'] += 1
            continue

        price = q.get('price')
        volume = q.get('volume', 0) or 0
        if not price or price <= 0:
            rej['no_quote'] += 1
            continue

        if price < params['min_price']:
            rej['price_too_low'] += 1
            continue
        if price > params['max_price']:
            rej['price_too_high'] += 1
            continue
        if volume < params['min_avg_volume']:
            rej['volume_too_low'] += 1
            continue

        # Drop check — if "change_30d_pct" is available, filter; if not, skip
        drop_30d = q.get('change_30d_pct')
        if drop_30d is not None and drop_30d < -params['max_drop_30d_pct']:
            rej['falling_knife'] += 1
            continue

        # News check
        if news_count_fn is not None and params['min_news_count_7d'] > 0:
            try:
                nc = news_count_fn(tu)
                if nc < params['min_news_count_7d']:
                    rej['no_news'] += 1
                    continue
            except Exception:
                pass

        candidates.append({
            'ticker': tu,
            'price': price,
            'volume': volume,
            'change_pct': q.get('change_pct', 0),
            'change_30d_pct': drop_30d,
            # v4.10.1: preserve quote source so snapshots can tell
            # whether data came from Yahoo or Stooq (the v4.9.4 fallback)
            'source': q.get('source'),
            'prev_close': q.get('prev_close'),
        })

    if progress_fn is not None:
        try: progress_fn(total, total, phase='filter_done')
        except TypeError:
            try: progress_fn(total, total)
            except Exception: pass
        except Exception: pass

    # Sort by liquidity (proxy for "interesting") — highest dollar volume first
    candidates.sort(
        key=lambda c: (c['price'] or 0) * (c['volume'] or 0),
        reverse=True
    )

    # v4.13.0: apply history-based re-ranking. Stocks the AI has been
    # consistently AVOID'ing recently get demoted; recently BUY'd stocks
    # get promoted. Tickers with no history pass through neutrally.
    # Pure pass-through if history_scores is None or scoring is disabled.
    if history_scores is not None:
        candidates = apply_history_scores(
            candidates, history_scores,
            enabled=history_scores_enabled)

    result = candidates[:max_results]

    # Stash rejection breakdown on the list for caller introspection
    # (using a function attribute since lists can't have attrs; do via closure
    # variable on the module instead)
    _last_filter_rejections.update(rej)

    # Store in pre-filter cache so a re-run with the same key is instant
    if cache_key is not None:
        _prefilter_cache_put(cache_key, result)

    # v4.10.0: write a scan snapshot if one is configured. Caller controls
    # the scan_id via cache_key (cache_key is typically (source, path, date)
    # — not unique enough for an scan_id by itself). Snapshot gets saved
    # under a fresh timestamp-based id. Failure is silent.
    # v4.10.1: also tag each candidate with universe membership so the
    # snapshot can be filtered by a different universe later without
    # re-fetching.
    try:
        snap = get_scan_snapshot()
        if snap is not None and result:
            scan_id = "scan_" + datetime.now().strftime('%Y%m%d_%H%M%S')
            universe_source = None
            path_used = path
            if cache_key and len(cache_key) >= 1:
                universe_source = str(cache_key[0])

            # v4.10.1: enrich each candidate with membership tags
            tagged_result = result
            if universe is not None:
                try:
                    tagged_result = []
                    for c in result:
                        c2 = dict(c)
                        try:
                            c2['memberships'] = universe.membership_for(
                                c.get('ticker', ''))
                        except Exception:
                            c2['memberships'] = []
                        tagged_result.append(c2)
                except Exception:
                    tagged_result = result  # fall back if anything blows up

            snap.save(
                scan_id=scan_id,
                candidates=tagged_result,
                universe_source=universe_source,
                path=path_used,
            )
    except Exception:
        # Never let snapshot save break the scan
        pass

    return result


# Module-level dict that holds the last filter run's rejection breakdown.
# Caller reads this AFTER filter_candidates returns to find out WHY tickers
# were excluded. Not thread-safe (don't run two concurrent filter_candidates
# calls), but that matches our usage.
_last_filter_rejections: dict[str, int] = {
    'held': 0,
    'no_quote': 0,
    'price_too_low': 0,
    'price_too_high': 0,
    'volume_too_low': 0,
    'falling_knife': 0,
    'no_news': 0,
}


def get_last_filter_rejections() -> dict[str, int]:
    """Return rejection breakdown from the most recent filter_candidates
    call. Useful for explaining "0 candidates" to the user."""
    return dict(_last_filter_rejections)


# ════════════════════════════════════════════════════════════════════════
# v4.8.12 — RATE-LIMIT COOLDOWN AUTO-TUNER
# ════════════════════════════════════════════════════════════════════════
#
# Persistent system that remembers when yfinance rate-limits us and
# refuses (or warns about) new batch scans until a cooldown expires.
# The cooldown duration auto-tunes based on observed history — uses the
# 80th percentile of recent recovery times so we err on the cautious side.
#
# State lives in data/rate_limit_state.json. Schema:
# {
#   "current_cooldown_min": 60,
#   "active_event": {  # null if no cooldown active
#     "detected_at": <iso>,
#     "severity_pct": 92.0,
#     "cooldown_until": <iso>,
#     "source": "yfinance"
#   },
#   "history": [  # last 20 events kept
#     {
#       "detected_at": <iso>,
#       "severity_pct": 92.0,
#       "cooldown_used_min": 60,
#       "recovered_at": <iso or null>,
#       "actual_recovery_min": <float or null>
#     },
#     ...
#   ]
# }

RATE_LIMIT_STATE_FILE: 'Path | None' = None
_rate_limit_state_lock = threading.Lock()
_rate_limit_log_fn = None  # Optional callback for activity log integration

# Tuning constants — kept conservative
COOLDOWN_DEFAULT_MIN = 60
COOLDOWN_FLOOR_MIN = 30
COOLDOWN_CEILING_MIN = 360  # 6 hours
COOLDOWN_HISTORY_KEEP = 20  # how many events to remember
COOLDOWN_LEARNING_WINDOW = 5  # use last N events for percentile
COOLDOWN_PERCENTILE = 0.80  # err on the cautious side


def set_rate_limit_state_path(path):
    """Wire the rate-limit state file. Pass None to disable persistence
    entirely (cooldown system becomes a no-op)."""
    global RATE_LIMIT_STATE_FILE
    RATE_LIMIT_STATE_FILE = path


def set_rate_limit_log_fn(fn):
    """Register a callback the cooldown system uses to surface diagnostic
    messages (e.g., into the app's activity log). fn takes one string arg."""
    global _rate_limit_log_fn
    _rate_limit_log_fn = fn


def _rl_log(msg: str):
    fn = _rate_limit_log_fn
    if fn is None:
        return
    try: fn(msg)
    except Exception: pass


def _rate_limit_load_state() -> dict:
    """Load persisted rate-limit state. Returns empty defaults on any
    error or missing file. Never raises."""
    p = RATE_LIMIT_STATE_FILE
    default = {
        'current_cooldown_min': COOLDOWN_DEFAULT_MIN,
        'active_event': None,
        'history': [],
    }
    if p is None or not p.exists():
        return default
    try:
        import json as _json
        data = _json.loads(p.read_text(encoding='utf-8'))
        # Validate shape
        if not isinstance(data, dict):
            return default
        for k in ('current_cooldown_min', 'active_event', 'history'):
            if k not in data:
                data[k] = default[k]
        if not isinstance(data['history'], list):
            data['history'] = []
        return data
    except Exception as e:
        _rl_log(f"Rate-limit state: could not load ({e}), using defaults")
        return default


def _rate_limit_save_state(state: dict):
    """Persist rate-limit state atomically. Best-effort, errors logged."""
    p = RATE_LIMIT_STATE_FILE
    if p is None:
        return
    try:
        import json as _json
        tmp = p.with_suffix(p.suffix + '.tmp')
        tmp.write_text(_json.dumps(state, indent=2), encoding='utf-8')
        tmp.replace(p)
    except Exception as e:
        _rl_log(f"Rate-limit state: could not save ({e})")


def _compute_next_cooldown_min(history: list) -> int:
    """Given a history of rate-limit events with recorded recovery times,
    compute the next cooldown duration via 80th-percentile of the last N
    events. Falls back to default if not enough data. Result is rounded
    up to the nearest 15 minutes and clamped to [floor, ceiling]."""
    # Only use events that actually recorded a recovery time
    with_recovery = [
        e for e in history
        if e.get('actual_recovery_min') is not None
        and e.get('actual_recovery_min') > 0
    ]
    if len(with_recovery) < 3:
        return COOLDOWN_DEFAULT_MIN

    recent = with_recovery[-COOLDOWN_LEARNING_WINDOW:]
    times = sorted(e['actual_recovery_min'] for e in recent)
    # 80th percentile
    idx = int(COOLDOWN_PERCENTILE * (len(times) - 1) + 0.5)
    p80 = times[idx]
    # Round up to nearest 15
    rounded = int(((p80 + 14.99) // 15) * 15)
    return max(COOLDOWN_FLOOR_MIN, min(COOLDOWN_CEILING_MIN, rounded))


def record_rate_limit_event(severity_pct: float, source: str = 'yfinance'):
    """Called when a rate-limit is detected. Starts a new cooldown period
    using the current auto-tuned duration. Persists the event."""
    if RATE_LIMIT_STATE_FILE is None:
        return
    with _rate_limit_state_lock:
        state = _rate_limit_load_state()
        cooldown_min = int(state.get('current_cooldown_min',
                                       COOLDOWN_DEFAULT_MIN))
        now = datetime.now()
        until = now + timedelta(minutes=cooldown_min)
        active = {
            'detected_at': now.isoformat(timespec='seconds'),
            'severity_pct': float(severity_pct),
            'cooldown_until': until.isoformat(timespec='seconds'),
            'source': source,
            'cooldown_used_min': cooldown_min,
        }
        state['active_event'] = active

        # Append to history (without recovery info yet)
        state['history'].append({
            'detected_at': active['detected_at'],
            'severity_pct': active['severity_pct'],
            'cooldown_used_min': cooldown_min,
            'recovered_at': None,
            'actual_recovery_min': None,
        })
        # Cap history
        if len(state['history']) > COOLDOWN_HISTORY_KEEP:
            state['history'] = state['history'][-COOLDOWN_HISTORY_KEEP:]

        _rate_limit_save_state(state)
        _rl_log(
            f"Rate-limit cooldown started: {source} flagged at "
            f"{severity_pct:.0f}% no-quote. Cooldown {cooldown_min}min, "
            f"until {until.strftime('%H:%M')} ET.")


def record_successful_scan(quote_success_pct: float = 100.0):
    """Called after a scan completes with healthy quote response rates.
    If we had an active cooldown, mark it recovered and recompute the
    next cooldown duration. Threshold for 'success' is >70% quotes."""
    if RATE_LIMIT_STATE_FILE is None:
        return
    if quote_success_pct < 70.0:
        return  # not actually a clean recovery, leave cooldown alone
    with _rate_limit_state_lock:
        state = _rate_limit_load_state()
        active = state.get('active_event')
        if active is None:
            return  # no cooldown active, nothing to record

        # Compute actual recovery time
        try:
            detected = datetime.fromisoformat(active['detected_at'])
            recovered = datetime.now()
            actual_min = (recovered - detected).total_seconds() / 60.0
        except Exception:
            actual_min = None
            recovered = datetime.now()

        # Update the matching history entry (last one with same detected_at)
        for entry in reversed(state['history']):
            if entry.get('detected_at') == active['detected_at']:
                entry['recovered_at'] = recovered.isoformat(
                    timespec='seconds')
                entry['actual_recovery_min'] = actual_min
                break

        # Clear active event
        state['active_event'] = None

        # Recompute next cooldown duration
        old_cooldown = state.get('current_cooldown_min',
                                   COOLDOWN_DEFAULT_MIN)
        new_cooldown = _compute_next_cooldown_min(state['history'])
        state['current_cooldown_min'] = new_cooldown

        _rate_limit_save_state(state)

        rec_str = (f"{actual_min:.0f}min" if actual_min is not None
                   else "unknown duration")
        if new_cooldown != old_cooldown:
            _rl_log(
                f"Rate-limit cleared after {rec_str}. Cooldown auto-tuned "
                f"from {old_cooldown}min to {new_cooldown}min based on "
                f"recent history.")
        else:
            _rl_log(
                f"Rate-limit cleared after {rec_str}. "
                f"Cooldown stays at {new_cooldown}min.")


def get_cooldown_status() -> dict:
    """Return the current cooldown status for UI display:
        {
          'active': bool,
          'remaining_min': int (0 if not active),
          'cooldown_until_str': str (HH:MM if active, else ''),
          'current_cooldown_min': int,
          'severity_pct': float (of active event),
          'history_count': int,
        }"""
    if RATE_LIMIT_STATE_FILE is None:
        return {
            'active': False, 'remaining_min': 0,
            'cooldown_until_str': '',
            'current_cooldown_min': COOLDOWN_DEFAULT_MIN,
            'severity_pct': 0.0, 'history_count': 0,
        }
    with _rate_limit_state_lock:
        state = _rate_limit_load_state()
    active = state.get('active_event')
    history_count = len(state.get('history', []))
    current_cd = state.get('current_cooldown_min', COOLDOWN_DEFAULT_MIN)
    if not active:
        return {
            'active': False, 'remaining_min': 0,
            'cooldown_until_str': '',
            'current_cooldown_min': current_cd,
            'severity_pct': 0.0, 'history_count': history_count,
        }
    try:
        until = datetime.fromisoformat(active['cooldown_until'])
        remaining_sec = (until - datetime.now()).total_seconds()
        if remaining_sec <= 0:
            # Cooldown expired but never explicitly cleared (no successful
            # scan happened). Treat as inactive but don't auto-clear —
            # leave the active_event for record_successful_scan to handle
            # if/when it comes.
            return {
                'active': False, 'remaining_min': 0,
                'cooldown_until_str': '',
                'current_cooldown_min': current_cd,
                'severity_pct': float(active.get('severity_pct', 0)),
                'history_count': history_count,
            }
        remaining_min = max(1, int(remaining_sec / 60))
        return {
            'active': True, 'remaining_min': remaining_min,
            'cooldown_until_str': until.strftime('%H:%M'),
            'current_cooldown_min': current_cd,
            'severity_pct': float(active.get('severity_pct', 0)),
            'history_count': history_count,
        }
    except Exception:
        return {
            'active': False, 'remaining_min': 0,
            'cooldown_until_str': '',
            'current_cooldown_min': current_cd,
            'severity_pct': 0.0, 'history_count': history_count,
        }


def reset_cooldown_learning():
    """Admin function: wipe history and reset cooldown to default. Used
    when the auto-tuner has tuned itself into a stupid value."""
    if RATE_LIMIT_STATE_FILE is None:
        return
    with _rate_limit_state_lock:
        state = {
            'current_cooldown_min': COOLDOWN_DEFAULT_MIN,
            'active_event': None,
            'history': [],
        }
        _rate_limit_save_state(state)
        _rl_log("Rate-limit cooldown learning reset to defaults.")


def detect_rate_limit_in_rejections(rej: dict) -> tuple[bool, float]:
    """Same threshold logic used in explain_zero_candidates. Returns
    (is_rate_limited, severity_pct). Exposed so callers can both
    explain AND record the event."""
    total = sum(rej.values())
    if total == 0:
        return (False, 0.0)
    no_quote = rej.get('no_quote', 0)
    pct = (no_quote / total * 100) if total else 0
    is_limited = pct > 40 and no_quote > 100
    return (is_limited, pct)


def explain_zero_candidates(rej: dict[str, int],
                              path: str,
                              universe_label: str = '') -> str:
    """Build a human-readable explanation of why no candidates passed
    the filter, given the rejection breakdown. Used in the activity
    log and in error messages.
    """
    params = PATH_FILTER_PARAMS.get(path, PATH_FILTER_PARAMS['moderate'])
    total_rejected = sum(rej.values())
    if total_rejected == 0:
        return "No tickers were checked (universe was empty)."

    # v4.8.11: detect yfinance rate-limiting. Signature: an unusually high
    # share of tickers fail with no_quote (Yahoo returned empty). Real
    # universes have <5% no-quote on a healthy day; >40% strongly suggests
    # rate-limiting rather than genuinely thin data. Surface this clearly
    # so the user knows to wait, not to retry immediately.
    no_quote_pct = (rej.get('no_quote', 0) / total_rejected * 100
                     if total_rejected else 0)
    if no_quote_pct > 40 and rej.get('no_quote', 0) > 100:
        return (
            f"Yahoo Finance appears to be rate-limiting us — "
            f"{rej.get('no_quote', 0)} of {total_rejected} tickers got no "
            f"quote ({no_quote_pct:.0f}%). This usually clears in 30-60 "
            f"minutes. Wait and try again, or rerun later.")

    # Find the dominant reason
    primary = max(rej, key=rej.get)
    primary_count = rej[primary]
    primary_pct = (primary_count / total_rejected * 100) if total_rejected else 0

    # Build a friendly explanation by reason
    parts = []
    if primary == 'price_too_high' and primary_pct > 60:
        ulab = universe_label or "this universe"
        parts.append(
            f"Path '{path}' filters for stocks under "
            f"${params['max_price']:g}, but {primary_count} of "
            f"{total_rejected} stocks in {ulab} are above that. "
            f"Try a different path (slow_safe = up to $1000, "
            f"moderate = up to $500), or scan a smaller-cap universe.")
    elif primary == 'price_too_low' and primary_pct > 60:
        parts.append(
            f"Path '{path}' filters for stocks above "
            f"${params['min_price']:g}, but most stocks in this universe "
            f"are below that. Try the lottery path "
            f"(min $0.10) or scan a different universe.")
    elif primary == 'volume_too_low' and primary_pct > 50:
        parts.append(
            f"Most stocks didn't meet the volume floor "
            f"({params['min_avg_volume']:,}). The universe may be stale "
            f"or include illiquid names. Try refreshing or switch path.")
    elif primary == 'falling_knife' and primary_pct > 50:
        parts.append(
            f"Most stocks were excluded as 'falling knives' "
            f"(down >{params['max_drop_30d_pct']:.0f}% in 30 days). "
            f"Bear market? Try aggressive or lottery path which allow "
            f"steeper drops.")
    else:
        # Multi-reason — just show the breakdown
        nonzero = [(k, v) for k, v in rej.items() if v > 0]
        nonzero.sort(key=lambda x: -x[1])
        breakdown = ", ".join(f"{k.replace('_', ' ')}: {v}"
                                for k, v in nonzero)
        parts.append(f"Rejection breakdown — {breakdown}")

    return " ".join(parts)


def check_path_universe_compat(path: str, source_key: str) -> tuple[bool, str]:
    """Quick pre-flight check: is this path/universe combo likely to
    return any candidates, or is it an obvious mismatch?

    Returns (is_compatible, warning_message). is_compatible=True means
    "should be fine, run it." False means "this almost certainly will
    return zero — explain to user before running."

    The check is heuristic, not exhaustive. False positives possible.
    """
    params = PATH_FILTER_PARAMS.get(path, PATH_FILTER_PARAMS['moderate'])
    max_p = params['max_price']
    min_p = params['min_price']

    # Heuristic typical price ranges per universe (very rough — based on
    # what's typically in each ETF). These are NOT hard rules, just
    # "what would a reasonable person expect."
    universe_typical = {
        # source_key: (typical_min, typical_max, label)
        'ivv': (15, 1500, 'S&P 500'),     # Large caps — Apple, MSFT, etc.
        'iwb': (5, 1500, 'Russell 1000'),  # Large + mid
        'iwm': (1, 500, 'Russell 2000'),   # Small caps
        'iwv': (1, 1500, 'Russell 3000'),  # Everything investable (small + mid + large)
        'qqq': (15, 1500, 'Nasdaq 100'),   # Tech-heavy large
        'vti': (1, 1500, 'Total Market'),  # Everything
    }

    info = universe_typical.get(source_key)
    if info is None:
        return (True, "")  # Unknown universe — give benefit of doubt

    typ_min, typ_max, ulab = info

    # If the path's max is below the universe's typical min, probably empty
    if max_p < typ_min:
        return (False,
            f"Path '{path}' filters for stocks under ${max_p:g}, but "
            f"{ulab} is mostly stocks above ${typ_min:g}. This combination "
            f"will likely return 0 results.")
    # If the path's min is above the universe's typical max, probably empty
    if min_p > typ_max:
        return (False,
            f"Path '{path}' filters for stocks above ${min_p:g}, but "
            f"{ulab} is mostly stocks under ${typ_max:g}. This combination "
            f"will likely return 0 results.")

    return (True, "")


# ─── Predictions ──────────────────────────────────────────────────────
# Standard prediction directions
DIRECTION_BUY = "BUY"
DIRECTION_HOLD = "HOLD"
DIRECTION_AVOID = "AVOID"
# v4.14.2 stage 4: WATCH is the candidate-prompt third option —
# replaces HOLD for non-owned tickers (HOLD is meaningless when
# there's no position to hold). Semantically: "interesting but
# wait for a better entry / clearer signal." WATCH never qualifies
# for the buy-recommendation list and is treated as inherently
# unresolved by the closer (no entry, no exit).
DIRECTION_WATCH = "WATCH"

# Confidence levels
CONFIDENCE_LOW = "LOW"
CONFIDENCE_MODERATE = "MODERATE"
CONFIDENCE_HIGH = "HIGH"

# Prediction outcome statuses
OUTCOME_OPEN = "open"               # still in window
OUTCOME_TARGET_HIT = "target_hit"   # price reached target before stop/expiry
OUTCOME_STOP_HIT = "stop_hit"       # price reached stop before target
OUTCOME_EXPIRED = "expired"         # timeframe lapsed without target/stop
OUTCOME_SOLD = "sold"               # user sold the position (Holdings only)
# v4.14.5.95-watch-phase2 (2026-06-11): a WATCH prediction whose
# thesis already played out — daily-bar history shows both the entry
# zone AND the target were touched since the WATCH was set. Stamped
# by tm_watch_resolution.classify_watch_progress + the Watching list
# auto-resolve pass so the stale setup is NOT re-offered as a fresh
# pick. Deliberately a SEPARATE status from target_hit: target_hit
# feeds Track Record accuracy math (BUY outcomes only); a WATCH that
# would have hit target if you'd bought at the entry is NOT a BUY
# success — conflating would corrupt the displayed accuracy %. The
# only downstream effects: tm_recommend_cache._is_current_buy excludes
# any non-open status (so the spent pick won't surface), and
# _recently_judged_set queries last_outcome IN ('BUY','WATCH','AVOID')
# (so a watch_resolved pick is naturally eligible for fresh re-
# analysis once Phase 1's short WATCH cutoff expires).
OUTCOME_WATCH_RESOLVED = "watch_resolved"
OUTCOME_CANCELLED = "cancelled"     # superseded by newer prediction
# v4.13.36: explicit closure types when a model retracts itself or
# another model in the same path contradicts. These count as failures
# for hit-rate purposes -- the prediction's premise was withdrawn
# before price could deliver a verdict.
OUTCOME_SUPERSEDED = "superseded"   # same model retracted within timeframe
OUTCOME_CONTRADICTED = "contradicted"  # different model in same path went AVOID/SELL
# v4.14.5.14-hold-grading: HOLD predictions (written by Refresh-Triggers since
# v4.14.5.14-refresh-triggers-writes-prediction) get their OWN verdict track,
# separate from the BUY target_hit/stop_hit accuracy denominator. A HOLD is a
# bet that the price STAYS IN ITS BAND over the timeframe.
OUTCOME_HOLD_HELD = "hold_held"      # band held to expiry → HOLD was correct
OUTCOME_HOLD_BROKEN = "hold_broken"  # target or stop breached → HOLD was wrong
# v4.14.5.14-trim-buy-more-grading: TRIM and BUY MORE are owned-position
# verdicts the refresh-triggers writer (_write_refresh_prediction, since
# v4.14.5.14-refresh-triggers-writes-prediction) persists VERBATIM, so they
# expire ungraded like HOLD did. They get their OWN verdict tracks, separate
# from the BUY target/stop denominator. TRIM = "lighten the position" (soft-
# bearish): correct on a decline (stop) or in-band plateau, wrong only on a
# surge past target. BUY MORE = "add at current levels" (stronger BUY): graded
# like BUY — target=correct, stop=incorrect, in-band expiry=inconclusive.
OUTCOME_TRIM_CORRECT = "trim_correct"        # decline/plateau → TRIM was right
OUTCOME_TRIM_INCORRECT = "trim_incorrect"    # surged past target → TRIM gave up upside
OUTCOME_BUY_MORE_CORRECT = "buy_more_correct"      # target hit → BUY MORE was right
OUTCOME_BUY_MORE_INCORRECT = "buy_more_incorrect"  # stop hit → added conviction wrongly
DIRECTION_TRIM = "TRIM"
# Owned-position consensus normalises the token to "BUY MORE" (space) and the
# winner is upper-cased; accept the spaceless/underscore variants defensively.
DIRECTIONS_BUY_MORE = frozenset({"BUY MORE", "BUYMORE", "BUY_MORE"})

# v4.14.5.1 (Step 3 — Recommend stability). Maturity guard: a BUY may
# not be superseded/contradicted until it has lived at least this long.
# Lets a thesis breathe instead of being killed within hours by the
# next clock-driven re-analysis. The legitimate market closer
# (check_outcomes: target/stop) is NOT affected by this — only the
# self-supersession path. Tunable after observing real stable behavior.
MIN_MATURITY_DAYS_FLOOR = 3            # absolute floor in days
MIN_MATURITY_TIMEFRAME_RATIO = 0.25   # or 25% of the prediction's timeframe

# ── v4.14.5.14-canonical-accuracy-definition ─────────────────────────
# THE single accuracy denominator definition, mirroring
# tm_source_accuracy._NON_ACCURACY_CLOSURES exactly so every display
# surface agrees with the source-weight bridge. A prediction's
# directional call is only DECIDED by the market when price reaches the
# target (hit) or the stop (miss). Every other closure is a non-verdict:
#   - superseded / contradicted: re-analysis closed it before price ruled
#   - expired: timeframe lapsed without a verdict ("we don't know")
#   - sold: a manual exit (has its own realized_win_rate_pct path)
#   - cancelled: never a real outcome
# These are EXCLUDED from the accuracy denominator. Counting them made
# patient-but-correct predictions look terrible (6% headline vs 38% true).
_ACCURACY_NON_DECIDED_CLOSURES = {
    'expired', 'superseded', 'contradicted', 'sold', 'cancelled'}


def _canonical_accuracy_decided(records):
    """Return (target_hits, stop_hits) — the canonical accuracy
    denominator is target_hits + stop_hits only. All other closures
    (see _ACCURACY_NON_DECIDED_CLOSURES) are non-verdicts and excluded.

    v4.14.5.62-validated-accuracy: shared attribution gate folded in (no-op
    when the use_validated_accuracy flag is off). Currently this helper has
    no live callers, but gate it so it stays consistent if wired up."""
    try:
        import tm_source_accuracy as _tsa_attr
        records = [r for r in records if _tsa_attr.is_attributable(r)]
    except Exception:
        pass
    target = sum(1 for r in records if r.get('status') == 'target_hit')
    stop = sum(1 for r in records if r.get('status') == 'stop_hit')
    return target, stop


# Regex patterns to extract structured fields from AI text. The AI is
# prompted to use clear labels; these patterns find them in the response.
# v4.14.6.7-verdict-parse-and-schema (2026-06-11): widen all structured
# patterns to tolerate markdown bold (`**LABEL:**`, `**VALUE**`) on
# either side of the colon, and accept STRONG BUY / STRONG SELL as
# verdict synonyms. Investigation captured live failing responses where
# Mistral/Gemini emit `**DIRECTION:** BUY` and the prior tight regex
# missed it — the verdict was present but invisible to the parser. The
# widening strictly extends the catchment: every previously-passing
# input still parses identically. STRONG is consumed via the non-
# capturing `(?:STRONG\s+)?` prefix so "STRONG BUY" → BUY, "STRONG
# SELL" → SELL (existing SELL→AVOID normalization at parse_prediction
# applies as today).
_PATTERN_DIRECTION = re.compile(
    r'(?:^|[\n\.\s\*])\**(?:DIRECTION|VERDICT|CALL|RECOMMEND(?:ATION)?)\**\s*[:\-]\s*\**\s*'
    # v4.14.2 stage 4: added WATCH for the candidate-prompt vocabulary
    # (BUY / WATCH / AVOID for non-owned tickers; HOLD remains valid
    # for owned-position prompts).
    r'(?:STRONG\s+)?(BUY|HOLD|AVOID|SELL|WATCH)\**',
    re.IGNORECASE | re.MULTILINE
)
_PATTERN_BUY_ZONE = re.compile(
    r'\**BUY[\s\-_]*ZONE\**\s*[:\-]\s*\**\s*\$?\s*([\d.]+)\s*(?:[-–to]+\s*\$?\s*([\d.]+))?\**',
    re.IGNORECASE
)
_PATTERN_TARGET = re.compile(
    r'\**(?:TARGET|TARGET\s*PRICE|GOAL)\**\s*[:\-]\s*\**\s*\$?\s*([\d.]+)\**',
    re.IGNORECASE
)
_PATTERN_STOP = re.compile(
    r'\**(?:STOP[\s\-_]*LOSS|STOP|EXIT[\s\-]*BELOW)\**\s*[:\-]\s*\**\s*\$?\s*([\d.]+)\**',
    re.IGNORECASE
)
_PATTERN_TIMEFRAME = re.compile(
    r'\**(?:TIMEFRAME|TIME[\s\-_]*HORIZON|HORIZON|EXPECTED[\s\-_]*TIME)\**'
    r'\s*[:\-]\s*\**\s*([\d]+)\s*(day|week|month)s?\**',
    re.IGNORECASE
)
_PATTERN_CONFIDENCE = re.compile(
    r'\**CONFIDENCE\**\s*[:\-]\s*\**\s*(LOW|MODERATE|MEDIUM|HIGH)\**',
    re.IGNORECASE
)


# v4.13.35 → v4.14.1 stage 0: earnings calendar -- used to stamp
# predictions with whether their evaluation window overlaps a
# scheduled earnings event for the ticker. Loaded lazily on first
# call. Format: dict[ticker_upper, list[event_dict]] where event
# dicts are Finnhub-shaped (ticker, date, eps_estimate, eps_actual,
# revenue_estimate, revenue_actual, hour, quarter, year). Pre-v4.14.1
# the source was a static Polygon snapshot at
# data/earnings_calendar.json with importance + fiscal_period
# fields; v4.14.1 retired that path in favor of live Finnhub via
# tm_data_router. _check_earnings_window only reads .get('date') and
# .get('importance'); the latter returns None gracefully for the
# new shape (importance is a future v4.14.x synthesis from
# market_cap; not in v4.14.1 scope).
#
# Graceful degradation: if Finnhub is unconfigured, network fails,
# rate limit hits, or the router returns empty, every prediction
# gets earnings_in_window=None ("unknown") and the rest of the
# system continues normally.
_EARNINGS_CALENDAR_CACHE = None
_EARNINGS_CALENDAR_LOADED = False
# v4.14.5.11: epoch of the last successful/attempted load. Drives the
# daily refresh (the per-process freeze used to keep a session's
# calendar stale forever; long sessions missed earnings reported that
# day). The hot fast-path latency is UNCHANGED — only the background
# daily tick passes force=True; arbitrary hot callers never pay a
# re-fetch.
_EARNINGS_CALENDAR_LOADED_AT = None
EARNINGS_CALENDAR_MAX_AGE_HOURS = 24
# v4.14.1 stage 0.1: serializes the first-load fetch so concurrent
# threads (e.g. parallel-provider consensus runs) don't race.
# Pre-stage-0 the load was an instant disk read, so the race
# window was effectively zero. Stage 0's network call has
# 500-1500ms latency, which widened the window to "always-hit
# during multi-provider consensus" and produced inconsistent
# earnings_in_window stamps across same-ticker votes. The lock
# closes the gap. Single global lock guards the single global
# cache — same pattern as the stdlib logging module.
_EARNINGS_CALENDAR_LOCK = threading.Lock()


def _load_earnings_calendar(force: bool = False):
    """v4.14.1 stage 0: load earnings calendar via tm_data_router
    (Finnhub adapter), bulk-fetch once per process, module-cache for
    the rest of the lifetime.

    Replaces the v4.13.35 path that read a static Polygon snapshot
    from data/earnings_calendar.json (manually populated, never
    refreshed in-process). Stage 0 retires that file path; the
    file itself is left on disk pending v4.14.x housekeeping.

    Bulk pattern: ONE Finnhub call covers a 200-day forward window
    for all tickers. Finnhub's /calendar/earnings endpoint is
    date-windowed, not ticker-windowed — passing ticker=None returns
    all events in the window, then we index client-side by ticker
    for fast lookup. days_ahead=200 (amended on 2026-05-08 from
    initially-locked 45) covers the full empirical timeframe
    distribution in predictions.jsonl, including the ~5% of records
    with 90- or 180-day timeframes that days_ahead=45 would
    silently miss. One Finnhub call regardless of scan size — much
    cheaper than per-ticker calls during a 50-candidate scan.

    Returns:
        dict[ticker_upper, list[event_dict_sorted_by_date]] on
        success. Empty dict on any error path (no Finnhub key,
        network failure, rate limit, empty response, router
        unavailable). The empty-dict fallback matches pre-v4.14.1
        graceful-degradation behavior — every prediction gets
        earnings_in_window=None ("unknown") rather than crashing.

    Thread safety (v4.14.1 stage 0.1):
        Double-checked locking pattern. Fast path returns the
        cached value WITHOUT acquiring the lock — every
        parse_prediction call after the first load is lock-free.
        Slow path serializes the first-load fetch via
        _EARNINGS_CALENDAR_LOCK so concurrent threads don't fire
        N redundant Finnhub calls AND don't see a half-populated
        cache. Threads that arrive while the fetch is in progress
        block on the lock; once the first thread completes, they
        re-check the LOADED flag inside the lock and return the
        populated cache.

    Module cache:
        _EARNINGS_CALENDAR_LOADED is set True early (before the
        fetch, inside the lock) so an exception during fetch
        doesn't cause subsequent threads to retry. To refresh,
        restart the app — TTL/refresh is deferred to v4.14.x.
    """
    # v4.14.5.14-earnings-architecture-fix-v2: RETIRED. The bulk ticker=None
    # Finnhub pre-fetch hit a 1500-event far-end truncation at the 200-day
    # window — it dropped all near-term earnings AND, routed through the hot
    # parse_prediction path, caused a startup fetch storm. Earnings are now
    # per-ticker in the tm_cache `earnings` table (see get_earnings_with_status
    # for the live path + get_earnings_for_ticker for the cache-only reads).
    # This stub remains only so any stray caller degrades gracefully to the
    # "unknown" empty-dict contract; it performs NO fetch.
    return {}


def _maybe_refresh_earnings_calendar(app=None) -> bool:
    """v4.14.5.14-earnings-architecture-fix-v2: RETIRED no-op. The old daily
    bulk re-fetch is gone — per-ticker freshness is now a 24h TTL on each
    `earnings` cache row (re-fetched lazily by the prompt path / the throttled
    fundfile seeder), so there is no calendar to age out and no wipe to cause a
    re-burst. Kept as a no-op for the fundfile daemon's existing call site."""
    return False


# ─── v4.14.5.14-earnings-architecture-fix-v2: DB-backed per-ticker earnings ──
#
# The bulk ticker=None Finnhub pre-fetch is RETIRED (it hit Finnhub's 1500-
# event far-end truncation → dropped near-term earnings → a startup fetch
# storm). Earnings now live per-ticker in the tm_cache `earnings` table
# (persisted → restart-safe, no module-cache wipe to re-burst). Two tiers:
#   - CACHE-ONLY readers (get_earnings_for_ticker, _check_earnings_window) used
#     by HOT bulk paths (parse_prediction over the universe, the earnings
#     triggers) — they NEVER fetch; a miss returns []/unknown.
#   - LIVE fetch (get_earnings_with_status) used ONLY by the bounded prompt-
#     build path (DataCacheLayer.earnings) + the throttled fundfile seeder.
# 3-state seed with TTL + backoff lives in the DB rows.

EARNINGS_CACHE_TTL_SECONDS = 24 * 3600          # ok freshness window
# 'failed' backoff curve by consecutive-failure count (capped at the last).
EARNINGS_FAILED_BACKOFF_SECONDS = [15 * 60, 30 * 60, 60 * 60, 2 * 3600, 6 * 3600]
# v4.14.5.28: CONFIRMED no-coverage ('empty'/'no_source'/ok-with-no-events)
# gets a long backoff via next_retry_at, so structurally-uncovered tickers
# (alt-class like BRKB, foreign like HEIA, pennies) aren't re-fetched every
# 24h. Mirrors the fundamentals empty pattern (tm_fundfile_fetcher
# FUND_EMPTY_TTL_DAYS = 7). Periodic, NOT permanent — a ticker that later
# gains coverage is picked up on the first post-window retry. Transient
# 'failed' (a source raised) keeps the SHORT escalating curve above and
# never gets this long window.
EARNINGS_EMPTY_BACKOFF_SECONDS = 7 * 86400


def _earnings_iso(ts):
    from datetime import datetime as _dt
    return _dt.fromtimestamp(ts).isoformat(timespec='seconds')


def _earnings_events_from_row(row):
    """Parse events_json from a tm_cache earnings row → list (never raises)."""
    if row is None:
        return []
    try:
        import json as _json
        raw = row['events_json'] if 'events_json' in row.keys() else None
        evs = _json.loads(raw) if raw else []
        return evs if isinstance(evs, list) else []
    except Exception:
        return []


def _earnings_cache_lookup(ticker):
    """CACHE-ONLY read → (events, status). status='unknown' when not seeded.
    NEVER fetches — the read primitive for the hot bulk paths."""
    try:
        import tm_cache
        row = tm_cache.get_earnings_cache((ticker or '').upper())
    except Exception:
        return [], 'unknown'
    if row is None:
        return [], 'unknown'
    return _earnings_events_from_row(row), (row['status'] or 'unknown')


def get_earnings_for_ticker(ticker: str) -> list[dict]:
    """CACHE-ONLY per-ticker earnings accessor (v2). Returns the cached event
    list, or [] if not seeded / empty / failed-with-no-prior-events. NEVER
    fetches — used by the earnings TRIGGER path (tm_event_triggers) +
    parse_prediction, both of which iterate many tickers and must not generate
    live API calls. The cache is filled by the bounded fundfile seeder + the
    prompt-build path (get_earnings_with_status)."""
    return _earnings_cache_lookup(ticker)[0]


def _earnings_seed_failed(ticker, row):
    """Record a 'failed' fetch with backoff; preserve any prior events. Never
    permanent-poison — the row stays re-fetchable after next_retry_at."""
    import time as _t
    try:
        import tm_cache
        prev_attempts = 0
        prev_events = '[]'
        prev_as_of = None
        if row is not None:
            try:
                prev_attempts = int(row['attempts'] or 0)
            except Exception:
                prev_attempts = 0
            pj = row['events_json'] if 'events_json' in row.keys() else None
            prev_events = pj if pj else '[]'
            prev_as_of = row['as_of'] if 'as_of' in row.keys() else None
        attempts = prev_attempts + 1
        idx = min(attempts - 1, len(EARNINGS_FAILED_BACKOFF_SECONDS) - 1)
        backoff = EARNINGS_FAILED_BACKOFF_SECONDS[idx]
        tm_cache.upsert_earnings_cache(
            ticker, events_json=prev_events, status='failed',
            as_of=prev_as_of, next_retry_at=_t.time() + backoff,
            attempts=attempts)
    except Exception:
        pass


def get_earnings_with_status(ticker: str):
    """LIVE per-symbol earnings fetch + DB seed (v2). Returns (events, status),
    status ∈ {'ok','empty','failed'}. PROMPT-BUILD (DataCacheLayer.earnings) +
    the throttled fundfile SEEDER ONLY — never call from a hot/bulk path.
    Serves fresh cache without fetching; fetches only on miss / past-TTL /
    past-backoff. Seeds 3-state: ok→events+as_of (24h TTL); empty→'[]'+as_of
    (24h TTL); failed→next_retry_at backoff, prior events preserved, never
    poisoned."""
    import time as _t, json as _json
    t = (ticker or '').upper()
    if not t:
        return [], 'empty'
    try:
        import tm_cache
        row = tm_cache.get_earnings_cache(t)
    except Exception:
        row = None
    now = _t.time()
    # Serve fresh cache without fetching.
    if row is not None:
        st = row['status'] or ''
        if st == 'ok':
            as_of = row['as_of']
            fresh = False
            try:
                from datetime import datetime as _dt
                fresh = (as_of is not None and
                         (now - _dt.fromisoformat(as_of).timestamp())
                         < EARNINGS_CACHE_TTL_SECONDS)
            except Exception:
                fresh = False
            if fresh:
                evs = _earnings_events_from_row(row)
                return evs, ('ok' if evs else 'empty')
        elif st == 'empty':
            # v4.14.5.28: confirmed no-coverage serves cached (NO fetch)
            # until next_retry_at (the 7d backoff). Pre-v4.14.5.28 rows have
            # no next_retry_at stamped → fall back to the 24h as_of TTL so
            # they re-confirm once, then get the long backoff written below.
            try:
                nra = float(row['next_retry_at'] or 0)
            except Exception:
                nra = 0
            if nra > 0:
                if now < nra:
                    return [], 'empty'
            else:
                try:
                    from datetime import datetime as _dt
                    as_of = row['as_of']
                    if (as_of is not None and
                            (now - _dt.fromisoformat(as_of).timestamp())
                            < EARNINGS_CACHE_TTL_SECONDS):
                        return [], 'empty'
                except Exception:
                    pass
        elif st == 'failed':
            try:
                nra = float(row['next_retry_at'] or 0)
            except Exception:
                nra = 0
            if now < nra:
                evs = _earnings_events_from_row(row)
                return evs, ('ok' if evs else 'failed')
    # Cache miss / stale / backoff elapsed → LIVE per-symbol fetch.
    try:
        import tm_data_router as _router
        r = _router.get_router()
        if r is None:
            _earnings_seed_failed(t, row)
            return _earnings_events_from_row(row), 'failed'
        payload, status = r.fetch('earnings', ticker=t, days_ahead=200,
                                   return_status=True)
    except Exception:
        _earnings_seed_failed(t, row)
        return _earnings_events_from_row(row), 'failed'
    if status == 'ok' and payload:
        events = (payload.get('result') or {}).get('events') or []
        if isinstance(events, list) and events:
            events = sorted(events, key=lambda e: e.get('date', ''))
            try:
                import tm_cache
                tm_cache.upsert_earnings_cache(
                    t, events_json=_json.dumps(events), status='ok',
                    as_of=_earnings_iso(now), source=payload.get('source'))
            except Exception:
                pass
            return events, 'ok'
        try:
            import tm_cache
            # ok response but zero events = confirmed no upcoming earnings
            # → long backoff (v4.14.5.28), same as 'empty'/'no_source'.
            tm_cache.upsert_earnings_cache(
                t, events_json='[]', status='empty', as_of=_earnings_iso(now),
                next_retry_at=now + EARNINGS_EMPTY_BACKOFF_SECONDS)
        except Exception:
            pass
        return [], 'empty'
    if status in ('empty', 'no_source'):
        try:
            import tm_cache
            # confirmed empty-from-all-sources → long backoff (v4.14.5.28)
            # so structurally-uncovered tickers aren't re-fetched every 24h.
            tm_cache.upsert_earnings_cache(
                t, events_json='[]', status='empty', as_of=_earnings_iso(now),
                next_retry_at=now + EARNINGS_EMPTY_BACKOFF_SECONDS)
        except Exception:
            pass
        return [], 'empty'
    # status == 'failed' → backoff, keep prior events, never poison.
    _earnings_seed_failed(t, row)
    return _earnings_events_from_row(row), 'failed'


def set_earnings_for_ticker(ticker: str, events: list[dict]) -> None:
    """v2 back-compat shim: seed the persisted earnings cache directly.
    Positive list → status 'ok'; empty list → confirmed 'empty'. The primary
    seeders are get_earnings_with_status (prompt/seeder, router-backed); this
    remains for any caller that already has events in hand."""
    import time as _t, json as _json
    t = (ticker or '').upper()
    if not t:
        return
    try:
        import tm_cache
        if events:
            evs = sorted(events, key=lambda e: e.get('date', ''))
            tm_cache.upsert_earnings_cache(
                t, events_json=_json.dumps(evs), status='ok',
                as_of=_earnings_iso(_t.time()))
        else:
            tm_cache.upsert_earnings_cache(
                t, events_json='[]', status='empty',
                as_of=_earnings_iso(_t.time()))
    except Exception:
        pass


def _check_earnings_window(ticker, timeframe_days=None, buffer_days=3):
    """CACHE-ONLY (v2): does `ticker`'s prediction window overlap a scheduled
    earnings event? NEVER fetches — reads the persisted earnings cache only
    (this runs inside parse_prediction, a hot bulk path). Window = [today,
    today + (timeframe_days or 30) + buffer_days].
    Returns {'in_window': bool|None, 'event_date': str|None, 'importance': None}
    — in_window None = unknown (not seeded / failed-with-no-data)."""
    try:
        t = (ticker or '').upper()
        events, status = _earnings_cache_lookup(t)
        if status == 'unknown' or (status == 'failed' and not events):
            return {'in_window': None, 'event_date': None, 'importance': None}
        if not events:
            return {'in_window': False, 'event_date': None, 'importance': None}
        from datetime import datetime as _dt35, timedelta as _td35
        today = _dt35.now().date()
        tf = timeframe_days if (timeframe_days and timeframe_days > 0) else 30
        window_end = today + _td35(days=tf + buffer_days)
        for e in events:
            try:
                ev_date = _dt35.strptime(
                    e.get('date', ''), '%Y-%m-%d').date()
            except (ValueError, TypeError):
                continue
            if today <= ev_date <= window_end:
                return {
                    'in_window': True,
                    'event_date': e.get('date'),
                    'importance': e.get('importance'),
                }
        return {'in_window': False, 'event_date': None,
                'importance': None}
    except Exception:
        return {'in_window': None, 'event_date': None,
                'importance': None}


def parse_prediction(text: str, ticker: str, current_price: float | None = None) -> dict:
    """Extract a structured prediction from AI response text.

    Returns dict with: direction, buy_zone_low, buy_zone_high, target,
    stop, timeframe_days, confidence, raw_text. Fields may be None if
    the AI didn't produce them clearly.

    The AI is prompted (via prompt builder) to use these specific labels.
    But parsing has to be tolerant of variation since LLMs go off-script.
    """
    pred = {
        'ticker': ticker.upper(),
        'direction': None,
        'buy_zone_low': None,
        'buy_zone_high': None,
        'target': None,
        'stop': None,
        'timeframe_days': None,
        'confidence': None,
        'current_price_at_prediction': current_price,
        'raw_text': text,
    }

    # Direction
    m = _PATTERN_DIRECTION.search(text)
    if m:
        d = m.group(1).upper()
        # Normalize SELL -> AVOID for new positions, but allow SELL for held
        if d == 'SELL':
            d = 'AVOID'
        pred['direction'] = d

    # Buy zone — single price or range
    m = _PATTERN_BUY_ZONE.search(text)
    if m:
        try:
            low = float(m.group(1))
            pred['buy_zone_low'] = low
            if m.group(2):
                pred['buy_zone_high'] = float(m.group(2))
            else:
                pred['buy_zone_high'] = low
        except ValueError:
            pass

    # Target
    m = _PATTERN_TARGET.search(text)
    if m:
        try:
            pred['target'] = float(m.group(1))
        except ValueError:
            pass

    # Stop loss
    m = _PATTERN_STOP.search(text)
    if m:
        try:
            pred['stop'] = float(m.group(1))
        except ValueError:
            pass

    # Timeframe
    m = _PATTERN_TIMEFRAME.search(text)
    if m:
        try:
            num = int(m.group(1))
            unit = m.group(2).lower()
            multipliers = {'day': 1, 'week': 7, 'month': 30}
            pred['timeframe_days'] = num * multipliers.get(unit, 1)
        except (ValueError, KeyError):
            pass

    # Confidence
    m = _PATTERN_CONFIDENCE.search(text)
    if m:
        c = m.group(1).upper()
        if c == 'MEDIUM':
            c = 'MODERATE'
        pred['confidence'] = c

    # v4.13.35: stamp earnings info on the prediction. Uses the parsed
    # timeframe if present, falls back to 30 days. The flag is checked
    # at prediction birth (now), so a 14-day prediction made today on a
    # ticker reporting in 8 days gets earnings_in_window=True. If the
    # calendar isn't loaded or doesn't cover this ticker, the field is
    # None ('unknown') and downstream filters can decide what to do.
    try:
        ew = _check_earnings_window(
            ticker, timeframe_days=pred.get('timeframe_days'))
        pred['earnings_in_window'] = ew.get('in_window')
        pred['earnings_event_date'] = ew.get('event_date')
        pred['earnings_importance'] = ew.get('importance')
    except Exception:
        pred['earnings_in_window'] = None
        pred['earnings_event_date'] = None
        pred['earnings_importance'] = None

    return pred


def _classify_sold_outcome(p: dict) -> str:
    """v4.14.5.14-sold-prediction-tracking: classify a 'sold' prediction
    as 'win' / 'loss' / 'flat' by comparing the exit price (close_price,
    set by mark_position_sold) to the entry. BUY-only (this is a long-only
    buy recommender); a non-BUY or any unparseable/missing price returns
    'flat' so it can NEVER fabricate a win or a loss. Entry mirrors the
    canonical compute_path_track_stats logic: buy-zone midpoint when both
    bounds exist, else current_price_at_prediction."""
    if (p.get('direction') or '').upper() != 'BUY':
        return 'flat'

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    lo = _f(p.get('buy_zone_low'))
    hi = _f(p.get('buy_zone_high'))
    entry = ((lo + hi) / 2.0 if (lo and hi)
             else _f(p.get('current_price_at_prediction')))
    exit_price = _f(p.get('close_price'))
    if entry is None or exit_price is None or entry <= 0:
        return 'flat'
    if exit_price > entry:
        return 'win'
    if exit_price < entry:
        return 'loss'
    return 'flat'


class PredictionsLog:
    """Append-only log of every AI prediction. Each entry includes:
        - id (timestamp-based)
        - timestamp (when made)
        - ticker, path
        - source ('holdings' / 'discover_watchlist' / 'discover_scan')
        - prediction fields (direction, buy_zone, target, stop, timeframe,
          confidence)
        - current_price_at_prediction
        - status ('open' / 'target_hit' / 'stop_hit' / 'expired' / 'sold' / 'cancelled')
        - closed_at (when status moved off 'open')
        - close_price (price at close)
        - notes (auto-populated when closed: "target hit at $X.XX")

    File: data/predictions.jsonl (one JSON object per line)

    v4.14.3.13 (2026-05-15): the file now supports DELTA records
    alongside full records, written by _persist_delta for status
    mutations on existing predictions. Shape:
        {"_d": 1, "id": "<pred_id>", "patch": {field: value, ...},
         "ts": "<iso8601>"}
    The _d=1 discriminator does not collide with any full-record
    field. _load merges deltas onto their referenced full record so
    every consumer of get_all() / get_open() sees the merged state.
    Compaction periodically rewrites the file with all-full-records
    (no deltas) to bound file size. Triggers: at __init__ after _load
    if delta_count/full_count > DELTA_RATIO_TRIGGER (0.5), and in-
    session after every DELTA_COMPACT_THRESHOLD (100) deltas.
    """

    # v4.14.3.13: compaction thresholds. Module-level constants so they
    # can be tuned without a cfg field. See investigation report for
    # rationale on the chosen values.
    DELTA_COMPACT_THRESHOLD = 100  # in-session deltas before compacting
    DELTA_RATIO_TRIGGER = 0.5      # startup compact if deltas/full > this

    # v4.14.6.34-lazy-predictions: hot-path working-set size. Loading
    # the entire predictions.jsonl on every launch is the only
    # remaining every-launch data-proportional cost after the
    # v4.14.6.33 async-startup refactor — a heavy user with a 29 MB
    # file (~10K records) pays ~500ms-2s + ~300 MB RAM at startup,
    # blocking the daemons that wait on holdings state to come up.
    # The hot path now loads:
    #   * the N most-recent records (by insertion order in the file),
    #     covering active analysis / recent-history reads, AND
    #   * EVERY record with status == OUTCOME_OPEN, regardless of age,
    #     because outcome resolution needs all unresolved predictions
    # The remaining older closed records stream into self._archive on
    # a background tail-load thread. Consumers needing full history
    # (accuracy weight maps, track record all-time view, history
    # scoring) call get_all_full(timeout=N) which blocks on the
    # _predictions_history_complete event. The append-only invariant
    # on self._cache is preserved so tm_queue_runner's len(_cache)-
    # based new-row detection (line ~4210, 4458) keeps working.
    HOT_PATH_RECENT_N = 2000

    def __init__(self, path: Path):
        self.path = path
        # v4.14.3.13: RLock instead of Lock. The pre-v4.14.3.13 code
        # had a dormant deadlock in mark_position_sold (held the lock
        # while calling _persist_full which tried to acquire it again).
        # Never triggered because the user-action path is rare, but
        # delta-append's new write patterns would have surfaced it
        # constantly. RLock makes both old and new patterns safe
        # without changing other locking semantics.
        self._lock = threading.RLock()
        # v4.14.3.13: in-session delta counter. Reset on compaction
        # and after _persist_full (which produces an all-full-records
        # file). Not persisted across launches - we re-count at startup
        # via _load's delta_count side-channel.
        self._delta_count_since_compact = 0
        # v4.14.6.34: split hot path vs. tail load. self._cache holds
        # the working set (last N + all open) and is append-only after
        # init. self._archive holds the older closed records once the
        # tail-load thread completes. get_all() returns archive+cache;
        # get_all_full() blocks on _predictions_history_complete.
        self._archive: list[dict] = []
        self._predictions_history_complete = threading.Event()
        # _archive_pending_ids: the set of record ids that need to be
        # loaded into archive. Populated by the hot-path scan; consumed
        # by the tail-load thread. Empty if every record fit in the
        # working set (new users / small files).
        self._archive_pending_ids: set[str] = set()

        # Hot-path load: working set only. Returns (working_cache,
        # delta_count, archive_pending_ids).
        (self._cache,
         _delta_count_at_load,
         self._archive_pending_ids) = self._load_working_set(
            self.HOT_PATH_RECENT_N)
        # Startup compaction trigger. If the file is more than half
        # deltas, rewrite as all-full-records before anyone else
        # touches it. Cheap insurance against a long-running uninter-
        # rupted session that didn't hit the in-session trigger.
        # NOTE: compaction must NOT run until the archive is loaded,
        # otherwise compact() would persist only the working set and
        # silently truncate the older records on disk. Defer the
        # decision until after tail-load completes (handled inside
        # the tail-load worker).
        self._startup_compaction_needed = (
            (len(self._cache) > 0
             and _delta_count_at_load / max(1, len(self._cache))
                 > self.DELTA_RATIO_TRIGGER)
            or (_delta_count_at_load > 0 and len(self._cache) == 0))

        # Spawn the background tail-load. If the file fit entirely in
        # the working set (new users), short-circuit to event-set and
        # skip the thread.
        if not self._archive_pending_ids:
            self._predictions_history_complete.set()
            # No deferred compaction needed in that case either —
            # the working set IS everything.
            if self._startup_compaction_needed:
                self.compact()
                self._startup_compaction_needed = False
        else:
            threading.Thread(
                target=self._tail_load_worker,
                daemon=True,
                name='predictions-tail-load').start()

    def _load(self) -> list[dict]:
        """Public-shape load: returns the merged record list.

        v4.14.3.13: backwards-compat shim; new internal callers use
        _load_with_counts to also receive the delta count for the
        startup compaction decision. Callers outside __init__ should
        still use this since the cache is built once at init."""
        records, _ = self._load_with_counts()
        return records

    # ── v4.14.6.34-lazy-predictions: hot-path / tail-load split ──

    def _read_merged_records(self) -> tuple[list[dict], list[str], int]:
        """Internal: walk predictions.jsonl applying deltas, return
        (insertion_ordered_records, insertion_order_ids, delta_count).

        Same merge algorithm as _load_with_counts (shared substrate)
        but separated so both the hot-path and tail-load paths can
        share it without code drift. On a missing file returns empty
        results.
        """
        if not self.path.exists():
            return [], [], 0
        records: dict[str, dict] = {}
        insertion_order: list[str] = []
        delta_count = 0
        orphan_count = 0
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get('_d') == 1:
                        delta_count += 1
                        pred_id = rec.get('id')
                        if not pred_id or pred_id not in records:
                            orphan_count += 1
                            continue
                        patch = rec.get('patch') or {}
                        if isinstance(patch, dict):
                            records[pred_id].update(patch)
                        continue
                    pred_id = rec.get('id')
                    rec.setdefault('provider_id', None)
                    rec.setdefault('canonical_model', None)
                    rec.setdefault('lineup_version', 'v4.13')
                    rec.setdefault('data_version', 'sparse')
                    if pred_id:
                        if pred_id not in records:
                            insertion_order.append(pred_id)
                        records[pred_id] = rec
                    else:
                        synthetic_key = (
                            f"__no_id__/{len(insertion_order)}")
                        insertion_order.append(synthetic_key)
                        records[synthetic_key] = rec
        except Exception:
            return [], [], 0
        if orphan_count > 0:
            try:
                print(
                    f"[PredictionsLog] {orphan_count} orphan delta "
                    f"record(s) skipped during load — file may have "
                    f"been hand-edited or partially corrupted.")
            except Exception:
                pass
        merged = [records[k] for k in insertion_order if k in records]
        return merged, insertion_order, delta_count

    def _load_working_set(
            self, recent_n: int
    ) -> tuple[list[dict], int, set[str]]:
        """v4.14.6.34 hot-path load. Walks the entire file (single
        pass) but builds the working set only:
          * the recent_n MOST-RECENTLY-INSERTED records, plus
          * every record with status == OUTCOME_OPEN regardless of
            position (so outcome resolution sees every unresolved
            prediction even if it's months old).

        Returns (working_cache, delta_count, archive_pending_ids)
        where archive_pending_ids is the set of record ids the
        background tail-loader still has to bring in.

        Insertion order within the returned working_cache mirrors the
        file's order, preserving tm_queue_runner's reverse-iteration
        recency assumptions.
        """
        merged, insertion_order, delta_count = self._read_merged_records()
        if not merged:
            return [], delta_count, set()

        n = len(insertion_order)
        # Recent-N cut: the LAST recent_n ids in insertion_order.
        if recent_n >= n:
            recent_ids = set(insertion_order)
        else:
            recent_ids = set(insertion_order[-recent_n:])

        # Build a quick id → record lookup over the merged list.
        # `merged` is in insertion order; the corresponding ids are in
        # insertion_order. Zip and reuse.
        id_to_rec = dict(zip(insertion_order, merged))

        # Pull in every open record regardless of recency. OUTCOME_OPEN
        # is the canonical "unresolved" marker; cover the legacy
        # "open" string too defensively.
        keep_ids: set[str] = set(recent_ids)
        for rid, rec in id_to_rec.items():
            st = (rec.get('status') or '')
            if st == OUTCOME_OPEN or st == 'open':
                keep_ids.add(rid)

        working = [id_to_rec[i] for i in insertion_order if i in keep_ids]
        pending = {
            i for i in insertion_order
            if i not in keep_ids and not i.startswith('__no_id__/')
        }
        return working, delta_count, pending

    def _tail_load_worker(self) -> None:
        """v4.14.6.34: background tail-load. Re-reads the file (full
        merge), filters down to JUST the archive_pending_ids, then
        atomically assigns self._archive. Honors the lock; never
        touches self._cache (append-only invariant kept).

        Sets self._predictions_history_complete on completion (always,
        even on error / empty result — so consumers waiting on the
        event are never wedged).
        """
        try:
            merged, insertion_order, _ = self._read_merged_records()
            pending = self._archive_pending_ids
            archive: list[dict] = []
            if merged and pending:
                id_to_rec = dict(zip(insertion_order, merged))
                archive = [
                    id_to_rec[i] for i in insertion_order
                    if i in pending and i in id_to_rec
                ]
            with self._lock:
                self._archive = archive
                # If startup wanted a compaction, do it now — after
                # we have the full record set so compact() rewrites
                # the whole file rather than just the working set.
                if self._startup_compaction_needed:
                    try:
                        self.compact()
                    except Exception:
                        pass
                    self._startup_compaction_needed = False
        except Exception:
            # Defensive: never let a tail-load fault prevent consumers
            # from making progress. The event is released in finally.
            pass
        finally:
            self._predictions_history_complete.set()

    def wait_for_full_history(self, timeout: float | None = 30.0) -> bool:
        """Block until the background tail-load completes. Returns
        True if loaded within the timeout, False on timeout.
        Consumers that walk all-time history (accuracy weight maps,
        track record all-time, history scoring) should call this
        before get_all() / direct _cache walks on the hot path.
        """
        return self._predictions_history_complete.wait(timeout)

    def _load_with_counts(self) -> tuple[list[dict], int]:
        """v4.14.3.13: read predictions.jsonl applying any delta
        records to their referenced full records. Returns:
            (merged_records_in_insertion_order, delta_count_seen)

        The merge algorithm:
          - Full records (no '_d' field) overwrite any prior entry
            for the same id.
          - Delta records (_d == 1) apply their 'patch' dict to the
            existing entry via dict.update(patch).
          - Orphan deltas (delta references an id not yet seen) log
            amber via print() and skip - shouldn't happen on a well-
            formed file, but defensive against manual edits or
            corruption.
          - Insertion order preserved via auxiliary list so
            get_recent_for_ticker's reverse-iteration semantics stay
            correct.
        """
        if not self.path.exists():
            return [], 0
        records: dict[str, dict] = {}
        insertion_order: list[str] = []
        delta_count = 0
        orphan_count = 0
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue

                    # v4.14.3.13: delta vs full discrimination.
                    if rec.get('_d') == 1:
                        delta_count += 1
                        pred_id = rec.get('id')
                        if not pred_id or pred_id not in records:
                            orphan_count += 1
                            continue
                        patch = rec.get('patch') or {}
                        if isinstance(patch, dict):
                            records[pred_id].update(patch)
                        continue

                    # Full record.
                    pred_id = rec.get('id')
                    # v4.13.65 forward-compat fill-ins. Older predictions
                    # written before the schema bump don't have these
                    # fields. Fill them in with the v4.13.65 defaults so
                    # every reader sees a uniform shape regardless of
                    # which app version wrote the record.
                    rec.setdefault('provider_id', None)
                    rec.setdefault('canonical_model', None)
                    rec.setdefault('lineup_version', 'v4.13')
                    rec.setdefault('data_version', 'sparse')
                    if pred_id:
                        if pred_id not in records:
                            insertion_order.append(pred_id)
                        records[pred_id] = rec
                    else:
                        # Record without id - shouldn't happen post-
                        # v4.13.65, but if it does, retain by synthesizing
                        # an order key from the file position. Use a
                        # tuple-style key the dict can hold but that
                        # won't collide with real pred_ids.
                        synthetic_key = f"__no_id__/{len(insertion_order)}"
                        insertion_order.append(synthetic_key)
                        records[synthetic_key] = rec
        except Exception:
            return [], 0

        if orphan_count > 0:
            # Amber via print (PredictionsLog has no app handle for
            # _log). The startup closer's diagnostic log will capture
            # this when it runs.
            try:
                print(
                    f"[PredictionsLog] {orphan_count} orphan delta "
                    f"record(s) skipped during load — file may have "
                    f"been hand-edited or partially corrupted.")
            except Exception:
                pass

        merged = [records[k] for k in insertion_order if k in records]
        return merged, delta_count

    def _persist_full(self):
        """Rewrite the whole file from cache. Used for STRUCTURAL
        operations (sites #1 + #2: restore_from_backup, clear_older_than)
        that can't be expressed as deltas. v4.14.3.13: per-status-
        mutation callers use _persist_delta instead - this method
        is for full-cache rewrites only."""
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, 'w') as f:
                    for entry in self._cache:
                        f.write(json.dumps(entry) + '\n')
                # v4.14.3.13: a full rewrite produces an all-full-
                # records file with no deltas. Reset the in-session
                # counter so the next compaction-trigger window
                # starts fresh.
                self._delta_count_since_compact = 0
            except Exception as e:
                # v4.14.3.13: surface the failure. Pre-v4.14.3.13 this
                # was a bare `except: pass` — silent failure here is
                # exactly the v4.13.37 closer-log issue all over again.
                try:
                    print(
                        f"[PredictionsLog] _persist_full failed: "
                        f"{type(e).__name__}: {e}")
                except Exception:
                    pass

    def _persist_delta(self, pred_id: str, patch: dict) -> None:
        """v4.14.3.13: append a delta record instead of rewriting the
        whole file. Each delta is ~200 bytes vs ~8.5MB for the full
        rewrite. Reader's _load_with_counts merges deltas back onto
        their referenced full record at next startup.

        Triggers in-session compaction when the counter crosses
        DELTA_COMPACT_THRESHOLD (100 deltas)."""
        if not pred_id:
            # Defensive: shouldn't happen since callers source pred_id
            # from existing cache entries. Log amber and skip rather
            # than write a garbage delta.
            try:
                print(
                    "[PredictionsLog] _persist_delta called with empty "
                    "pred_id; ignoring")
            except Exception:
                pass
            return

        delta_entry = {
            '_d': 1,
            'id': pred_id,
            'patch': dict(patch),
            'ts': datetime.now().isoformat(timespec='seconds'),
        }

        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(delta_entry) + '\n')
                self._delta_count_since_compact += 1
                # In-session compaction trigger. Fires under the
                # same lock so a write that lands the 100th delta
                # immediately collapses the file before releasing.
                # Compaction is cheap (~80-130ms on current file
                # size) so the lock-hold time is acceptable.
                if (self._delta_count_since_compact
                        >= self.DELTA_COMPACT_THRESHOLD):
                    self._compact_locked()
            except Exception as e:
                try:
                    print(
                        f"[PredictionsLog] _persist_delta failed for "
                        f"{pred_id}: {type(e).__name__}: {e}")
                except Exception:
                    pass

    def compact(self) -> bool:
        """v4.14.3.13: rewrite predictions.jsonl as all-full-records
        (no deltas) via temp file + atomic rename. Public entry point
        for startup compaction and explicit user-triggered cleanup.
        Returns True on success.

        Crash safety: the original file is NOT modified until
        os.replace succeeds. If the temp-write fails or the rename
        fails, the original is intact and the temp file is cleaned
        up. the user's data is never at risk.
        """
        with self._lock:
            return self._compact_locked()

    def _compact_locked(self) -> bool:
        """Internal compaction — assumes self._lock is already held.
        Use compact() externally."""
        import os as _os
        try:
            # Snapshot the merged state. _cache is already merged.
            snapshot = list(self._cache)
        except Exception as e:
            try:
                print(
                    f"[PredictionsLog] compact: cache snapshot failed: "
                    f"{type(e).__name__}: {e}")
            except Exception:
                pass
            return False

        temp_path = self.path.with_suffix(
            self.path.suffix + '.compact_tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp_path, 'w', encoding='utf-8') as f:
                for entry in snapshot:
                    f.write(json.dumps(entry) + '\n')
                # fsync before rename guarantees data is on disk
                # before the atomic swap.
                try:
                    f.flush()
                    _os.fsync(f.fileno())
                except Exception:
                    # fsync failure is non-fatal but worth a log line.
                    try:
                        print(
                            "[PredictionsLog] compact: fsync failed "
                            "before rename; data may not be durable.")
                    except Exception:
                        pass
            # os.replace is atomic on Windows same-volume rename per
            # Python 3.3+ docs (uses MoveFileEx with
            # MOVEFILE_REPLACE_EXISTING).
            _os.replace(str(temp_path), str(self.path))
            self._delta_count_since_compact = 0
            return True
        except Exception as e:
            try:
                print(
                    f"[PredictionsLog] compact failed: "
                    f"{type(e).__name__}: {e}; original file intact")
            except Exception:
                pass
            # Cleanup the orphan temp file.
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
            return False

    def append(self, entry: dict) -> str:
        """Add a new prediction. Returns the prediction id."""
        # Generate ID
        ts = datetime.now()
        pred_id = ts.strftime('%Y%m%d_%H%M%S_%f')
        full_entry = {
            'id': pred_id,
            'timestamp': ts.isoformat(),
            'status': OUTCOME_OPEN,
            'closed_at': None,
            'close_price': None,
            'notes': '',
            # v4.13.65 schema bump: forward-compat fields for the
            # v4.14.0 routing rework. Defaults are safe ("we don't
            # know yet") so existing call sites don't have to change.
            # Cloud paths that already pass `provider_id` keep working
            # because **entry below overrides these defaults.
            'provider_id': None,
            'canonical_model': None,
            'lineup_version': 'v4.13',
            'data_version': 'v4.14.1',
            **entry,
        }

        with self._lock:
            self._cache.append(full_entry)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, 'a') as f:
                    f.write(json.dumps(full_entry) + '\n')
            except Exception:
                pass
        return pred_id

    def get_open(self, ticker: str | None = None) -> list[dict]:
        """Return open (uncllosed) predictions, optionally filtered by
        ticker."""
        with self._lock:
            out = [p for p in self._cache if p.get('status') == OUTCOME_OPEN]
        if ticker:
            tu = ticker.upper()
            out = [p for p in out if p.get('ticker', '').upper() == tu]
        return out

    def get_all(self) -> list[dict]:
        """Return every record currently in memory: tail-loaded
        archive (older closed) PLUS the live working set (recent +
        all open + appended since startup). Initially the archive is
        empty so this returns the working set only; once the
        background tail-load completes, it returns the full history.

        Callers that REQUIRE the full history (accuracy weight maps,
        track record all-time, history scoring) should call
        get_all_full() instead — it blocks on
        _predictions_history_complete to guarantee completeness.
        """
        with self._lock:
            return list(self._archive) + list(self._cache)

    def get_all_full(self, timeout: float | None = 30.0) -> list[dict]:
        """v4.14.6.34: blocking variant of get_all() that waits on
        the background tail-load. Use this for any consumer walking
        all-time history. Returns the merged record list once full
        history is available; on timeout, returns whatever's in
        memory (best-effort — log the timeout to surface stalls).
        """
        if not self.wait_for_full_history(timeout):
            try:
                print(
                    "[PredictionsLog] get_all_full timed out waiting "
                    "for tail-load; returning partial history.")
            except Exception:
                pass
        return self.get_all()

    def get_most_recent_for_ticker_and_path(
            self, ticker: str, path: str) -> Optional[dict]:
        """v4.14.4.1 (2026-05-15): return the most recent prediction
        record for (ticker, path) regardless of direction (BUY /
        WATCH / AVOID / HOLD / NO_CALL).

        Used by the event-driven price-drift sweep
        (tm_event_triggers.check_price_triggers) to determine the
        baseline price + target/stop for drift computation. The
        baseline is "what price did we last anchor analysis to for
        this (ticker, path)" — direction doesn't matter; if we said
        WATCH at $150 and now it's $165, that's a 10% drift worth
        re-evaluating.

        Returns None if no matching prediction exists. Iterates
        self._cache in REVERSE (newest first) for early exit;
        v4.14.3.13's delta-append + merge logic guarantees _cache
        order matches insertion order so reverse iteration finds
        the latest record without sorting.
        """
        tu = (ticker or '').upper()
        pu = (path or '').strip()
        if not tu or not pu:
            return None
        with self._lock:
            for rec in reversed(self._cache):
                if rec.get('ticker', '').upper() != tu:
                    continue
                if (rec.get('path') or '').strip() != pu:
                    continue
                return rec
        return None

    def clear_all(self, backup: bool = True) -> int:
        """Delete all predictions. If backup=True (default), the current
        file is copied to predictions_backup_TIMESTAMP.jsonl before
        deletion, so users can recover if they regret it.

        Returns the number of predictions that were cleared.
        """
        with self._lock:
            count = len(self._cache)
            if backup and self.path.exists():
                try:
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    backup_path = self.path.parent / f"predictions_backup_{ts}.jsonl"
                    import shutil
                    shutil.copy2(self.path, backup_path)
                except Exception:
                    pass
            self._cache = []
            try:
                if self.path.exists():
                    self.path.unlink()
            except Exception:
                pass
        return count

    def _restored_marker_path(self):
        """v4.8.14: marker file recording which backups have been restored
        and when. Lives next to predictions.jsonl as
        restored_backups.json. Format: {backup_name: iso_timestamp}."""
        return self.path.parent / 'restored_backups.json'

    def _load_restored_markers(self) -> dict:
        p = self._restored_marker_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return {}

    def _save_restored_markers(self, markers: dict):
        p = self._restored_marker_path()
        try:
            tmp = p.with_suffix(p.suffix + '.tmp')
            tmp.write_text(json.dumps(markers, indent=2), encoding='utf-8')
            tmp.replace(p)
        except Exception:
            pass

    def list_backups(self) -> list[dict]:
        """v4.8.11: List available prediction backup files in the data
        directory. Returns a list of dicts sorted newest-first:
            {'path': Path, 'name': str, 'timestamp': str,
             'count': int, 'size': int, 'restored_at': str | None}
        Backups are predictions_backup_YYYYMMDD_HHMMSS.jsonl files.
        Count is the number of prediction lines in each backup.

        v4.8.14: 'restored_at' field added — ISO timestamp of when this
        backup was last restored (via restore_from_backup), or None if
        never restored. Helps the user tell which backups they've
        already merged so they don't re-merge by accident.
        """
        markers = self._load_restored_markers()
        out = []
        try:
            data_dir = self.path.parent
            for bp in data_dir.glob('predictions_backup_*.jsonl'):
                try:
                    # Extract timestamp from filename
                    stem = bp.stem  # predictions_backup_20260427_174414
                    ts_part = stem.replace('predictions_backup_', '')
                    # Friendly format
                    if len(ts_part) >= 15:  # YYYYMMDD_HHMMSS
                        ts_friendly = (
                            f"{ts_part[:4]}-{ts_part[4:6]}-{ts_part[6:8]} "
                            f"{ts_part[9:11]}:{ts_part[11:13]}:{ts_part[13:15]}")
                    else:
                        ts_friendly = ts_part
                    # Count predictions in the backup
                    count = 0
                    try:
                        with open(bp) as bf:
                            for line in bf:
                                if line.strip():
                                    count += 1
                    except Exception:
                        pass
                    out.append({
                        'path': bp,
                        'name': bp.name,
                        'timestamp': ts_friendly,
                        'raw_ts': ts_part,
                        'count': count,
                        'size': bp.stat().st_size,
                        'restored_at': markers.get(bp.name),  # v4.8.14
                    })
                except Exception:
                    continue
        except Exception:
            return []
        # Newest first
        out.sort(key=lambda b: b.get('raw_ts', ''), reverse=True)
        return out

    def restore_from_backup(self, backup_path,
                              mode: str = 'merge') -> tuple[int, int]:
        """v4.8.11: Restore predictions from a backup file.

        backup_path: Path to predictions_backup_*.jsonl
        mode:
          'merge' — load backup entries, merge with current (dedupe by 'id'
                    if present, else by (ticker, timestamp) tuple)
          'replace' — first auto-back up the current log, then overwrite
                      with the backup contents

        Returns (added, total_after) — number of new predictions actually
        added to the log, and the total count after restore.
        """
        backup_path = Path(backup_path)
        if not backup_path.exists():
            return (0, len(self._cache))

        # Read the backup
        backup_entries = []
        try:
            with open(backup_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        backup_entries.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            return (0, len(self._cache))

        with self._lock:
            if mode == 'replace':
                # Back up current first (so they can undo a bad restore)
                if self.path.exists():
                    try:
                        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                        pre_restore = self.path.parent / (
                            f"predictions_backup_{ts}_pre_restore.jsonl")
                        import shutil
                        shutil.copy2(self.path, pre_restore)
                    except Exception:
                        pass
                self._cache = list(backup_entries)
                added = len(backup_entries)
            else:
                # Merge: dedupe
                existing_ids = set()
                existing_keys = set()
                for p in self._cache:
                    pid = p.get('id')
                    if pid:
                        existing_ids.add(pid)
                    else:
                        existing_keys.add(
                            (p.get('ticker', ''), p.get('timestamp', '')))
                added = 0
                for entry in backup_entries:
                    eid = entry.get('id')
                    ekey = (entry.get('ticker', ''),
                             entry.get('timestamp', ''))
                    if eid and eid in existing_ids:
                        continue
                    if not eid and ekey in existing_keys:
                        continue
                    self._cache.append(entry)
                    added += 1
                    if eid:
                        existing_ids.add(eid)
                    else:
                        existing_keys.add(ekey)
            total = len(self._cache)

        # Persist outside the lock (calls re-acquire it)
        self._persist_full()

        # v4.8.14: record that this backup was restored, so the dialog
        # can show "already restored at HH:MM" next to it next time.
        try:
            markers = self._load_restored_markers()
            markers[backup_path.name] = datetime.now().isoformat(
                timespec='seconds')
            self._save_restored_markers(markers)
        except Exception:
            pass

        return (added, total)


    def clear_older_than(self, days: int, backup: bool = True) -> int:
        """Delete predictions older than `days` days. Keeps recent ones.
        Returns the count cleared. Auto-backup like clear_all."""
        cutoff = datetime.now() - timedelta(days=days)
        with self._lock:
            if backup and self.path.exists():
                try:
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    backup_path = self.path.parent / f"predictions_backup_{ts}.jsonl"
                    import shutil
                    shutil.copy2(self.path, backup_path)
                except Exception:
                    pass
            kept = []
            cleared = 0
            for p in self._cache:
                ts_str = p.get('timestamp', '')
                if not ts_str:
                    kept.append(p)  # No timestamp = keep
                    continue
                try:
                    pts = datetime.fromisoformat(ts_str)
                    if pts >= cutoff:
                        kept.append(p)
                    else:
                        cleared += 1
                except (ValueError, TypeError):
                    kept.append(p)  # Bad timestamp = keep
            self._cache = kept
        # Rewrite file from new cache
        self._persist_full()
        return cleared

    def cleanup_tiered(self, *, dry_run: bool = True, now=None,
                       backup_dir=None) -> dict:
        """v4.14.5.14-predictions-cleanup: tiered retention cleanup.

        Retention by direction:
          - BUY                : kept FOREVER (the source-weight bridge
                                 reads only closed BUYs; open BUYs are
                                 live signals). Never dropped on age.
          - NO_CALL            : kept 1 day (pure 'couldn't decide' noise).
          - WATCH/AVOID/HOLD   : kept 30 days (21-day staleness window + 9d).
          - unknown direction / missing-or-bad timestamp : KEPT (failsafe).

        dry_run=True  -> classify + return counts; DO NOT mutate or write.
        dry_run=False -> back up self.path (rotating, last 7), drop the
                         aged-out non-BUY records, atomic-rewrite the file.

        Returns a counts dict (same shape both modes). Never raises; on
        backup/write failure it ABORTS without deleting (counts['aborted']
        = True, file left intact)."""
        from datetime import datetime as _dt, timedelta as _td
        ref = now or _dt.now()
        NO_CALL_MAX = _td(days=1)
        OTHER_MAX = _td(days=30)
        counts = {
            'kept_buy': 0, 'kept_no_call_fresh': 0, 'kept_other_fresh': 0,
            'kept_unknown': 0, 'kept_no_timestamp': 0,
            'dropped_no_call_old': 0, 'dropped_other_old': 0,
            'total_before': 0, 'total_after': 0, 'aborted': False,
        }
        with self._lock:
            src = list(self._cache)
            counts['total_before'] = len(src)
            kept = []
            for p in src:
                d = (p.get('direction') or '').upper()
                if d == DIRECTION_BUY:
                    kept.append(p)
                    counts['kept_buy'] += 1
                    continue
                ts_str = p.get('timestamp') or ''
                if not ts_str:
                    kept.append(p)
                    counts['kept_no_timestamp'] += 1
                    continue
                try:
                    age = ref - _dt.fromisoformat(ts_str)
                except (ValueError, TypeError):
                    kept.append(p)
                    counts['kept_no_timestamp'] += 1
                    continue
                if d == 'NO_CALL':
                    if age < NO_CALL_MAX:
                        kept.append(p)
                        counts['kept_no_call_fresh'] += 1
                    else:
                        counts['dropped_no_call_old'] += 1
                elif d in ('WATCH', 'AVOID', 'HOLD'):
                    if age < OTHER_MAX:
                        kept.append(p)
                        counts['kept_other_fresh'] += 1
                    else:
                        counts['dropped_other_old'] += 1
                else:
                    kept.append(p)
                    counts['kept_unknown'] += 1
            counts['total_after'] = len(kept)
            dropped = (counts['dropped_no_call_old']
                       + counts['dropped_other_old'])
            # Dry-run, or nothing to drop: never mutate / write / back up.
            if dry_run or dropped == 0:
                return counts
            # Real cleanup — back up first, then atomic rewrite. Either
            # failure ABORTS without deleting anything.
            try:
                self._backup_for_cleanup(backup_dir)
            except Exception as e:
                counts['aborted'] = True
                try:
                    print(f"[PredictionsLog] cleanup backup failed; "
                          f"aborting (no records deleted): {e}")
                except Exception:
                    pass
                return counts
            try:
                self._atomic_rewrite(kept)
            except Exception as e:
                counts['aborted'] = True
                try:
                    print(f"[PredictionsLog] cleanup atomic write failed; "
                          f"aborting (backup intact, file unchanged): {e}")
                except Exception:
                    pass
                return counts
            self._cache = kept
            self._delta_count_since_compact = 0
        return counts

    def _backup_for_cleanup(self, backup_dir=None) -> None:
        """Copy the current predictions file to data/backups/ with a
        per-DATE name (same-day reruns overwrite rather than accumulate),
        rotating to the last 7. Raises on copy failure so the caller
        aborts before deleting anything."""
        import os
        import shutil
        from datetime import datetime as _dt
        if not self.path.exists():
            return
        bdir = backup_dir or (self.path.parent / 'backups')
        os.makedirs(str(bdir), exist_ok=True)
        today = _dt.now().strftime('%Y-%m-%d')
        dest = os.path.join(str(bdir),
                            f'predictions_pre_cleanup_{today}.jsonl')
        shutil.copy2(str(self.path), dest)
        # Rotate: keep the last 7 dated pre-cleanup backups.
        try:
            existing = sorted(
                f for f in os.listdir(str(bdir))
                if f.startswith('predictions_pre_cleanup_')
                and f.endswith('.jsonl'))
            while len(existing) > 7:
                oldest = existing.pop(0)
                try:
                    os.remove(os.path.join(str(bdir), oldest))
                except Exception:
                    pass
        except Exception:
            pass

    def _atomic_rewrite(self, records) -> None:
        """Atomic full rewrite of predictions.jsonl from `records`
        (temp + flush + fsync + os.replace). Raises on failure; the temp
        is cleaned up and the original left intact. Caller holds _lock."""
        import os
        tmp = self.path.with_name(self.path.name + '.cleanup.tmp')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                for entry in records:
                    f.write(json.dumps(entry) + '\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(self.path))
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            raise

    def get_recent_for_ticker(self, ticker: str,
                               days_back: int = 90,
                               limit: int = 5) -> list[dict]:
        """Get recent predictions for a ticker. Used to inject context
        into new prompts ('here's what you predicted last time')."""
        tu = ticker.upper()
        cutoff = datetime.now() - timedelta(days=days_back)
        out = []
        with self._lock:
            for p in reversed(self._cache):  # newest first
                if p.get('ticker', '').upper() != tu:
                    continue
                try:
                    ts = datetime.fromisoformat(p.get('timestamp', ''))
                    if ts < cutoff:
                        break
                except Exception:
                    continue
                out.append(p)
                if len(out) >= limit:
                    break
        return out

    def update_outcome(self, pred_id: str, status: str,
                        close_price: float | None = None,
                        notes: str = "") -> bool:
        """Mark a prediction as closed with an outcome.

        v4.14.3.13: writes a delta record (~200 bytes) instead of
        rewriting the whole file. See _persist_delta."""
        with self._lock:
            for p in self._cache:
                if p.get('id') == pred_id:
                    patch = {
                        'status': status,
                        'closed_at': datetime.now().isoformat(),
                        'close_price': close_price,
                        'notes': notes,
                    }
                    p.update(patch)
                    self._persist_delta(pred_id, patch)
                    return True
        return False

    def patch_record(self, pred_id: str, patch: dict) -> bool:
        """v4.14.5.62-validated-accuracy: ADDITIVELY merge `patch` onto the
        prediction record `pred_id` and persist it via the same schema-
        agnostic delta path the closer/stop-recovery-sweep use. Does NOT
        touch status/closed_at — for stamping side-channel fields like
        `tier2_validation`. Returns True if the record was found. Never
        raises into the caller."""
        if not pred_id or not isinstance(patch, dict) or not patch:
            return False
        try:
            with self._lock:
                for p in self._cache:
                    if p.get('id') == pred_id:
                        p.update(patch)
                        self._persist_delta(pred_id, patch)
                        return True
        except Exception:
            pass
        return False

    # v4.13.15 (revised v4.14.6.0-price-band-tiers): per-tier expiration
    # FALLBACK when an AI prediction omits its own `timeframe_days`. The
    # AI-provided per-pick horizon is the primary; this only fires when
    # the parser couldn't extract it. Price tier doesn't dictate a
    # holding window, so the new bands all use a uniform 30-day
    # fallback. Legacy time-path keys remain as aliases (they'll
    # resolve to the same fallback when persisted state still names
    # them).
    PATH_EXPIRATION_DAYS = {
        'lottery':       30,
        'band_5_10':     30,
        'band_10_50':    30,
        'band_50_up':    30,
        'aggressive':    30,
        'moderate':      30,
        'slow_safe':     30,
        'penny_lottery': 30,
    }

    def check_outcomes(self, quote_fn: Callable[[str], dict | None],
                        history_fn: Callable[[str], Any] | None = None
                        ) -> list[dict]:
        """Walk through open predictions and update any that have hit
        target / stop / expired. Returns list of updated entries.

        v4.13.15: WICK-AWARE evaluation. If history_fn is provided, it
        is used to get daily OHLC candles (returning a list of dicts
        with 'date', 'close' fields, sorted oldest-first). Target/stop
        hits then require a DAILY CLOSE beyond the level, not just
        an intraday wick. Falls back to spot quote_fn if history_fn
        not provided (legacy behavior, less accurate).

        v4.13.15: Per-path expiration fallback. If prediction has no
        timeframe_days, falls back to PATH_EXPIRATION_DAYS based on
        the prediction's path.
        """
        now = datetime.now()
        updated = []
        with self._lock:
            cache_copy = list(self._cache)

        # Group by ticker so we fetch history ONCE per ticker, then
        # evaluate ALL open predictions for that ticker against it.
        # (Saves a lot of yfinance calls.)
        open_by_ticker: dict[str, list[dict]] = {}
        for p in cache_copy:
            if p.get('status') != OUTCOME_OPEN:
                continue
            t = (p.get('ticker') or '').upper()
            if not t:
                continue
            open_by_ticker.setdefault(t, []).append(p)

        for ticker, preds in open_by_ticker.items():
            # Try to get daily history for wick-aware evaluation
            history = None
            if history_fn is not None:
                try:
                    history = history_fn(ticker)
                except Exception:
                    history = None

            # Always try spot quote too, for the close_note + as
            # fallback for tickers history_fn fails on
            try:
                q = quote_fn(ticker)
            except Exception:
                q = None
            current_price = (q or {}).get('price')

            for p in preds:
                # Resolve made_at
                try:
                    made_at = datetime.fromisoformat(p.get('timestamp', ''))
                except Exception:
                    continue

                target = p.get('target')
                stop = p.get('stop')
                direction = (p.get('direction') or '').upper()

                outcome = None
                close_note = ""
                close_price_for_record = current_price

                if direction == DIRECTION_BUY:
                    # Wick-aware path: walk daily candles since made_at
                    if history and isinstance(history, list):
                        for candle in history:
                            try:
                                cdate = candle.get('date')
                                cclose = float(candle.get('close', 0))
                            except Exception:
                                continue
                            if not cdate or cclose <= 0:
                                continue
                            # Skip candles BEFORE prediction was made
                            try:
                                cdate_dt = (
                                    datetime.fromisoformat(cdate)
                                    if isinstance(cdate, str)
                                    else cdate)
                            except Exception:
                                continue
                            if cdate_dt < made_at:
                                continue
                            # Did this day's close cross target or stop?
                            if (target is not None
                                    and cclose >= float(target)):
                                outcome = OUTCOME_TARGET_HIT
                                close_note = (
                                    f"target ${target:g} hit; "
                                    f"daily close ${cclose:.2f} on "
                                    f"{cdate}")
                                close_price_for_record = cclose
                                break
                            if (stop is not None
                                    and cclose <= float(stop)):
                                outcome = OUTCOME_STOP_HIT
                                close_note = (
                                    f"stop ${stop:g} hit; "
                                    f"daily close ${cclose:.2f} on "
                                    f"{cdate}")
                                close_price_for_record = cclose
                                break
                    elif current_price is not None:
                        # Fallback: spot-quote evaluation (legacy)
                        if (target is not None
                                and current_price >= float(target)):
                            outcome = OUTCOME_TARGET_HIT
                            close_note = (
                                f"target ${target:g} hit at "
                                f"${current_price:g} (spot)")
                        elif (stop is not None
                                and current_price <= float(stop)):
                            outcome = OUTCOME_STOP_HIT
                            close_note = (
                                f"stop ${stop:g} hit at "
                                f"${current_price:g} (spot)")

                elif direction == DIRECTION_HOLD and (
                        target is not None or stop is not None):
                    # v4.14.5.14-hold-grading: a HOLD bets the price STAYS
                    # in its band. A target OR stop breach means HOLD was
                    # the wrong call (BUY or SELL would have done better) →
                    # hold_broken. Surviving to expiry → hold_held (in the
                    # expiry block below). Same wick-aware walk as BUY.
                    if history and isinstance(history, list):
                        for candle in history:
                            try:
                                cdate = candle.get('date')
                                cclose = float(candle.get('close', 0))
                            except Exception:
                                continue
                            if not cdate or cclose <= 0:
                                continue
                            try:
                                cdate_dt = (
                                    datetime.fromisoformat(cdate)
                                    if isinstance(cdate, str) else cdate)
                            except Exception:
                                continue
                            if cdate_dt < made_at:
                                continue
                            if (target is not None
                                    and cclose >= float(target)):
                                outcome = OUTCOME_HOLD_BROKEN
                                close_note = (
                                    f"HOLD band broken — target ${target:g} "
                                    f"reached (daily close ${cclose:.2f} on "
                                    f"{cdate}); BUY would have won")
                                close_price_for_record = cclose
                                break
                            if (stop is not None
                                    and cclose <= float(stop)):
                                outcome = OUTCOME_HOLD_BROKEN
                                close_note = (
                                    f"HOLD band broken — stop ${stop:g} "
                                    f"reached (daily close ${cclose:.2f} on "
                                    f"{cdate}); SELL would have won")
                                close_price_for_record = cclose
                                break
                    elif current_price is not None:
                        if (target is not None
                                and current_price >= float(target)):
                            outcome = OUTCOME_HOLD_BROKEN
                            close_note = (
                                f"HOLD band broken — target ${target:g} "
                                f"reached at ${current_price:g} (spot)")
                        elif (stop is not None
                                and current_price <= float(stop)):
                            outcome = OUTCOME_HOLD_BROKEN
                            close_note = (
                                f"HOLD band broken — stop ${stop:g} "
                                f"reached at ${current_price:g} (spot)")

                elif direction == DIRECTION_TRIM and (
                        target is not None or stop is not None):
                    # v4.14.5.14-trim-buy-more-grading: TRIM = "lighten the
                    # position." Soft-bearish, so correct on a decline (stop)
                    # or an in-band plateau (graded at expiry below); wrong
                    # only if the price SURGES past target (the upside the
                    # user gave up by trimming). Same wick-aware daily-close
                    # walk as BUY/HOLD; target is checked first so a surge
                    # registers as incorrect.
                    if history and isinstance(history, list):
                        for candle in history:
                            try:
                                cdate = candle.get('date')
                                cclose = float(candle.get('close', 0))
                            except Exception:
                                continue
                            if not cdate or cclose <= 0:
                                continue
                            try:
                                cdate_dt = (
                                    datetime.fromisoformat(cdate)
                                    if isinstance(cdate, str) else cdate)
                            except Exception:
                                continue
                            if cdate_dt < made_at:
                                continue
                            if (target is not None
                                    and cclose >= float(target)):
                                outcome = OUTCOME_TRIM_INCORRECT
                                close_note = (
                                    f"TRIM wrong — target ${target:g} reached "
                                    f"(daily close ${cclose:.2f} on {cdate}); "
                                    f"holding would have captured the upside")
                                close_price_for_record = cclose
                                break
                            if (stop is not None
                                    and cclose <= float(stop)):
                                outcome = OUTCOME_TRIM_CORRECT
                                close_note = (
                                    f"TRIM right — stop ${stop:g} reached "
                                    f"(daily close ${cclose:.2f} on {cdate}); "
                                    f"trimming saved capital")
                                close_price_for_record = cclose
                                break
                    elif current_price is not None:
                        if (target is not None
                                and current_price >= float(target)):
                            outcome = OUTCOME_TRIM_INCORRECT
                            close_note = (
                                f"TRIM wrong — target ${target:g} reached at "
                                f"${current_price:g} (spot)")
                        elif (stop is not None
                                and current_price <= float(stop)):
                            outcome = OUTCOME_TRIM_CORRECT
                            close_note = (
                                f"TRIM right — stop ${stop:g} reached at "
                                f"${current_price:g} (spot)")

                elif direction in DIRECTIONS_BUY_MORE and (
                        target is not None or stop is not None):
                    # v4.14.5.14-trim-buy-more-grading: BUY MORE = a stronger
                    # BUY ("add at current levels"). Graded like BUY: target
                    # reached → correct, stop reached → incorrect. Expiring
                    # in-band is INCONCLUSIVE (OUTCOME_EXPIRED below), not a
                    # loss — neither the added conviction nor the doubt was
                    # confirmed. Same wick-aware walk as BUY.
                    if history and isinstance(history, list):
                        for candle in history:
                            try:
                                cdate = candle.get('date')
                                cclose = float(candle.get('close', 0))
                            except Exception:
                                continue
                            if not cdate or cclose <= 0:
                                continue
                            try:
                                cdate_dt = (
                                    datetime.fromisoformat(cdate)
                                    if isinstance(cdate, str) else cdate)
                            except Exception:
                                continue
                            if cdate_dt < made_at:
                                continue
                            if (target is not None
                                    and cclose >= float(target)):
                                outcome = OUTCOME_BUY_MORE_CORRECT
                                close_note = (
                                    f"BUY MORE right — target ${target:g} "
                                    f"reached (daily close ${cclose:.2f} on "
                                    f"{cdate})")
                                close_price_for_record = cclose
                                break
                            if (stop is not None
                                    and cclose <= float(stop)):
                                outcome = OUTCOME_BUY_MORE_INCORRECT
                                close_note = (
                                    f"BUY MORE wrong — stop ${stop:g} reached "
                                    f"(daily close ${cclose:.2f} on {cdate})")
                                close_price_for_record = cclose
                                break
                    elif current_price is not None:
                        if (target is not None
                                and current_price >= float(target)):
                            outcome = OUTCOME_BUY_MORE_CORRECT
                            close_note = (
                                f"BUY MORE right — target ${target:g} reached "
                                f"at ${current_price:g} (spot)")
                        elif (stop is not None
                                and current_price <= float(stop)):
                            outcome = OUTCOME_BUY_MORE_INCORRECT
                            close_note = (
                                f"BUY MORE wrong — stop ${stop:g} reached at "
                                f"${current_price:g} (spot)")

                # Check expiry. v4.13.15: fall back to per-path default
                # if no explicit timeframe_days.
                if outcome is None:
                    tf_days = p.get('timeframe_days')
                    if not tf_days:
                        path_key = (p.get('path') or '').strip().lower()
                        tf_days = self.PATH_EXPIRATION_DAYS.get(
                            path_key, 30)
                    expiry = made_at + timedelta(days=int(tf_days))
                    if now >= expiry:
                        # v4.14.5.14-hold-grading: a HOLD that reached its
                        # full timeframe without breaching target/stop held
                        # its band → HOLD was correct (hold_held), not a
                        # generic non-verdict expiry. A HOLD with no band
                        # (no target AND no stop) can't be graded → it still
                        # expires as a non-verdict, same as before.
                        if (direction == DIRECTION_HOLD
                                and (target is not None or stop is not None)):
                            outcome = OUTCOME_HOLD_HELD
                            close_note = (
                                f"HOLD band held {tf_days}d "
                                f"(no target/stop breach)")
                            if current_price is not None:
                                close_note += f"; price ${current_price:g}"
                        elif (direction == DIRECTION_TRIM
                                and (target is not None or stop is not None)):
                            # v4.14.5.14-trim-buy-more-grading: a TRIM that
                            # rode to expiry without surging past target was
                            # right — trimming captured profit at a fair price
                            # without missing a rally. (A BUY MORE that expires
                            # in-band falls through to OUTCOME_EXPIRED below:
                            # inconclusive, deliberately NOT counted as wrong.)
                            outcome = OUTCOME_TRIM_CORRECT
                            close_note = (
                                f"TRIM right — held {tf_days}d with no surge "
                                f"past target; trim captured profit at a fair "
                                f"price")
                            if current_price is not None:
                                close_note += f"; price ${current_price:g}"
                        else:
                            outcome = OUTCOME_EXPIRED
                            if current_price is not None:
                                close_note = (
                                    f"timeframe expired ({tf_days}d); "
                                    f"price at ${current_price:g}")
                            else:
                                close_note = (
                                    f"timeframe expired ({tf_days}d)")

                if outcome:
                    with self._lock:
                        for cp in self._cache:
                            if cp.get('id') == p.get('id'):
                                # v4.14.3.13: build the patch dict
                                # once, apply to in-memory cache,
                                # write delta. Same mutation set as
                                # pre-v4.14.3.13; just a different
                                # persistence mechanism.
                                _patch = {
                                    'status': outcome,
                                    'closed_at': now.isoformat(),
                                    'close_price': close_price_for_record,
                                    'notes': close_note,
                                    'eval_method': (
                                        'wick_aware'
                                        if history else 'spot_quote'),
                                }
                                cp.update(_patch)
                                self._persist_delta(
                                    cp.get('id', ''), _patch)
                                updated.append(dict(cp))
                                break

        # v4.14.3.13: per-record deltas are written inline above; no
        # end-of-loop full rewrite needed. The 'updated' list is
        # still returned for the caller's reporting purposes.
        return updated

    def sweep_stop_recovery(self, history_fn=None, now=None) -> dict:
        """v4.14.5.62-stop-recovery: ADDITIVE track-record honesty sweep.

        A stop_hit is recorded the instant a daily close crosses the stop
        (check_outcomes — UNCHANGED, the exit is honored exactly as today).
        That flat verdict HIDES whether price then RECOVERED back above the
        stop within the prediction's OWN horizon — i.e. whether this model's
        stops are set too tight. This sweep revisits already-closed stop_hit
        records whose ORIGINAL horizon has FULLY elapsed
        (made_at + timeframe_days <= now), reconstructs the window between
        the stop-hit date and the original expiry, and stamps an ADDITIVE
        `stop_recovery` observation.

        It changes NO verdict and touches NO open prediction — it only ADDS
        a `stop_recovery` key to matured stop_hit records, persisted exactly
        the way the closer persists outcomes (cp.update + _persist_delta).
        Idempotent: a record already carrying stop_recovery is skipped, so
        it's stamped ONCE — at first maturity, when candle coverage is
        freshest (coverage only degrades as the 60-day window slides on).

        `recovered` is TRI-STATE by design — never assert "no recovery"
        when candle coverage is missing:
          True  — a daily close ABOVE the stop was observed in the window.
          False — coverage is FULL and no such close occurred (confident).
          None  — coverage is partial/unavailable and none was seen
                  (unknown: we lack the candles to be sure).

        Returns a summary dict for the caller's reporting. Never raises on a
        per-record fault (best-effort, like the closer)."""
        from datetime import datetime as _dt, timedelta as _td
        if now is None:
            now = _dt.now()

        with self._lock:
            cache_copy = list(self._cache)

        def _pdate(s):
            try:
                return _dt.fromisoformat(str(s).split('T')[0]).date()
            except Exception:
                return None

        # ── 1. select MATURED stop_hit records lacking stop_recovery ──
        candidates = []  # (record, made_at, expiry_date)
        for p in cache_copy:
            if p.get('status') != OUTCOME_STOP_HIT:
                continue
            if 'stop_recovery' in p:
                continue  # idempotent — already swept
            try:
                made_at = _dt.fromisoformat(p.get('timestamp', ''))
            except Exception:
                continue  # unparseable made_at → can't judge maturity; skip
            tf = p.get('timeframe_days')
            if not tf:
                path_key = (p.get('path') or '').strip().lower()
                tf = self.PATH_EXPIRATION_DAYS.get(path_key, 30)
            try:
                expiry = made_at + _td(days=int(tf))
            except Exception:
                continue
            if expiry > now:
                continue  # horizon not yet elapsed → too early; later sweep
            candidates.append((p, made_at, expiry))

        summary = {
            'scanned': len(cache_copy),
            'matured_candidates': len(candidates),
            'stamped': 0, 'recovered': 0, 'not_recovered': 0,
            'unknown_coverage': 0, 'by_model': {}}
        if not candidates:
            return summary

        # ── 2. group by ticker; fetch candles ONCE per ticker ──
        by_ticker: dict = {}
        for tup in candidates:
            t = (tup[0].get('ticker') or '').upper()
            by_ticker.setdefault(t, []).append(tup)

        for ticker, tups in by_ticker.items():
            candles = None
            if history_fn is not None and ticker:
                try:
                    candles = history_fn(ticker)
                except Exception:
                    candles = None
            # Normalize → sorted ascending [(date, close)]
            norm = []
            if isinstance(candles, list):
                for c in candles:
                    try:
                        d = _pdate(c.get('date'))
                        cl = float(c.get('close', 0))
                    except Exception:
                        continue
                    if d is None or cl <= 0:
                        continue
                    norm.append((d, cl))
            norm.sort(key=lambda x: x[0])
            cov_lo = norm[0][0] if norm else None
            cov_hi = norm[-1][0] if norm else None

            for (p, made_at, expiry) in tups:
                try:
                    stop_f = (float(p.get('stop'))
                              if p.get('stop') is not None else None)
                except Exception:
                    stop_f = None
                w_start = _pdate(p.get('closed_at')) or made_at.date()
                w_end = expiry.date()

                # coverage: is the WHOLE window inside the fetched range?
                if not norm:
                    coverage = 'unavailable'
                elif cov_lo <= w_start and cov_hi >= w_end:
                    coverage = 'full'
                else:
                    coverage = 'partial'

                # candles strictly AFTER the stop-hit day, up to expiry
                in_window = [(d, cl) for (d, cl) in norm
                             if d > w_start and d <= w_end]
                recovery_date = None
                max_close = None
                if stop_f is not None and in_window:
                    for (d, cl) in in_window:  # ascending → first = earliest
                        if cl > stop_f:
                            recovery_date = d
                            break
                    max_close = max(cl for (_d, cl) in in_window)
                # price on/nearest-before expiry (context)
                before_end = [cl for (d, cl) in norm if d <= w_end]
                price_at_expiry = before_end[-1] if before_end else None

                # tri-state recovered (never assert False without coverage)
                if recovery_date is not None:
                    recovered = True
                elif coverage == 'full' and stop_f is not None:
                    recovered = False
                else:
                    recovered = None

                days_to_recover = None
                if recovery_date is not None:
                    try:
                        days_to_recover = (recovery_date - w_start).days
                    except Exception:
                        days_to_recover = None

                stamp = {
                    'recovered': recovered,
                    'recovery_date': (recovery_date.isoformat()
                                      if recovery_date else None),
                    'days_to_recover': days_to_recover,
                    'price_at_expiry': price_at_expiry,
                    'max_close_in_window': max_close,
                    'coverage': coverage,
                    'window_start': w_start.isoformat(),
                    'window_end': w_end.isoformat(),
                    'stop': stop_f,
                    'swept_at': now.isoformat(timespec='seconds'),
                    'version': 'v4.14.5.62',
                }
                _patch = {'stop_recovery': stamp}
                with self._lock:
                    for cp in self._cache:
                        if cp.get('id') == p.get('id'):
                            cp.update(_patch)
                            self._persist_delta(cp.get('id', ''), _patch)
                            break

                summary['stamped'] += 1
                mdl = p.get('model') or '?'
                mb = summary['by_model'].setdefault(
                    mdl, {'recovered': 0, 'not_recovered': 0, 'unknown': 0})
                if recovered is True:
                    summary['recovered'] += 1
                    mb['recovered'] += 1
                elif recovered is False:
                    summary['not_recovered'] += 1
                    mb['not_recovered'] += 1
                else:
                    summary['unknown_coverage'] += 1
                    mb['unknown'] += 1

        return summary

    def check_supersessions(self, stable: bool = True,
                            now=None) -> list[dict]:
        """v4.13.36: Walk open BUY predictions and close any that have
        been retracted by their author or contradicted by another model
        in the same path within the prediction's timeframe.

        v4.14.5.1 (Step 3 — Recommend stability). When `stable` is True
        (cfg['use_stable_recommend'], default True):
          - Tier 1 SUPERSEDED is PATH-SCOPED: a non-BUY by the same
            model only supersedes a prior BUY *under the same path*.
            Cross-path verdicts answer different questions (a stock can
            be BUY under aggressive and HOLD under slow_safe by design)
            and must not collide. Legacy records with no path use ''
            as a sentinel that cannot match a current path.
          - A MATURITY GUARD applies to BOTH tiers: a BUY younger than
            max(MIN_MATURITY_DAYS_FLOOR, timeframe_days *
            MIN_MATURITY_TIMEFRAME_RATIO) is skipped entirely.
        When `stable` is False, legacy path-blind, no-maturity behavior
        is fully restored (rollback safety).

        Two tiers, applied in order so a prediction is closed by the
        clearer signal:

        1. SUPERSEDED: same model, same ticker, opposing direction
           (HOLD / TRIM / AVOID / SELL) within the original timeframe.
           Author has self-retracted -- count as failure.

        2. CONTRADICTED: different model in same path, same ticker,
           opposing AVOID or SELL (not HOLD or TRIM, those are weaker
           and could still be consistent with a long BUY thesis) within
           the original timeframe. Stronger threshold for cross-model
           since different models legitimately have different views.

        Returns the list of newly-closed predictions.
        """
        from datetime import timedelta as _td36
        with self._lock:
            cache_copy = list(self._cache)

        # Index: same-model lookups by (model, ticker)
        # and path-level lookups by (path, ticker)
        same_model_idx: dict = {}
        path_idx: dict = {}
        for p in cache_copy:
            t = (p.get('ticker') or '').upper()
            m = (p.get('model') or '').strip()
            path = (p.get('path') or '').strip()
            d = (p.get('direction') or '').upper()
            ts = p.get('timestamp') or ''
            if not (t and ts and d):
                continue
            if m:
                same_model_idx.setdefault((m, t), []).append(p)
            if path:
                path_idx.setdefault((path, t), []).append(p)

        # Sort each bucket newest-first for fast lookup
        for k in same_model_idx:
            same_model_idx[k].sort(key=lambda x: x.get('timestamp', ''))
        for k in path_idx:
            path_idx[k].sort(key=lambda x: x.get('timestamp', ''))

        opposing = ('HOLD', 'TRIM', 'AVOID', 'SELL')
        strong_opposing = ('AVOID', 'SELL')
        now_dt = now if now is not None else datetime.now()
        updated = []

        for p in cache_copy:
            if p.get('status') != OUTCOME_OPEN:
                continue
            if (p.get('direction') or '').upper() != DIRECTION_BUY:
                continue
            t = (p.get('ticker') or '').upper()
            m = (p.get('model') or '').strip()
            path = (p.get('path') or '').strip()
            ts_str = p.get('timestamp') or ''
            try:
                made_at = datetime.fromisoformat(ts_str)
            except Exception:
                continue
            tf_days = p.get('timeframe_days')
            try:
                tf_days = int(tf_days) if tf_days else 30
            except (ValueError, TypeError):
                tf_days = 30
            window_end = made_at + _td36(days=tf_days)

            # v4.14.5.1 maturity guard (both tiers). A BUY must live
            # long enough to be judged before self-supersession can
            # close it. Does NOT gate the market closer (check_outcomes).
            if stable:
                try:
                    age_days = (now_dt - made_at).total_seconds() / 86400.0
                except Exception:
                    age_days = 1e9  # unparseable age -> don't protect
                maturity_days = max(
                    MIN_MATURITY_DAYS_FLOOR,
                    tf_days * MIN_MATURITY_TIMEFRAME_RATIO)
                if age_days < maturity_days:
                    continue  # too young to supersede/contradict yet

            # Tier 1: same model self-retraction
            superseded_by = None
            if m:
                for other in same_model_idx.get((m, t), []):
                    if other is p:
                        continue
                    od = (other.get('direction') or '').upper()
                    if od not in opposing:
                        continue
                    # v4.14.5.1: path-scope Tier 1. A non-BUY under a
                    # DIFFERENT path is answering a different question
                    # and must not supersede this BUY. '' (legacy
                    # no-path) only matches '' — cannot collide with a
                    # current path value.
                    if stable and (other.get('path') or '').strip() != path:
                        continue
                    ots = other.get('timestamp', '')
                    try:
                        otime = datetime.fromisoformat(ots)
                    except Exception:
                        continue
                    if otime <= made_at:
                        continue
                    if otime > window_end:
                        continue
                    superseded_by = other
                    break

            if superseded_by:
                # v4.14.3.13: build patch + apply + emit delta. Same
                # mutation set as pre-v4.14.3.13; different persistence
                # mechanism (~200-byte append vs 8.5MB rewrite).
                _patch = {
                    'status': OUTCOME_SUPERSEDED,
                    'closed_at': superseded_by.get('timestamp'),
                    'close_note': (
                        f"superseded -- same model ({m}) said "
                        f"{superseded_by.get('direction')} on "
                        f"{(superseded_by.get('timestamp') or '')[:10]}"),
                    'superseded_by_id': superseded_by.get('id'),
                }
                p.update(_patch)
                self._persist_delta(p.get('id', ''), _patch)
                updated.append(p)
                continue

            # Tier 2: cross-model contradiction in same path (strong only)
            contradicted_by = None
            if path:
                for other in path_idx.get((path, t), []):
                    if other is p:
                        continue
                    om = (other.get('model') or '').strip()
                    if om == m:
                        continue
                    od = (other.get('direction') or '').upper()
                    if od not in strong_opposing:
                        continue
                    ots = other.get('timestamp', '')
                    try:
                        otime = datetime.fromisoformat(ots)
                    except Exception:
                        continue
                    if otime <= made_at:
                        continue
                    if otime > window_end:
                        continue
                    contradicted_by = other
                    break

            if contradicted_by:
                # v4.14.3.13: build patch + apply + emit delta. Same
                # mutation set as pre-v4.14.3.13; different persistence.
                _patch = {
                    'status': OUTCOME_CONTRADICTED,
                    'closed_at': contradicted_by.get('timestamp'),
                    'close_note': (
                        f"contradicted -- "
                        f"{contradicted_by.get('model','?')} said "
                        f"{contradicted_by.get('direction')} on "
                        f"{(contradicted_by.get('timestamp') or '')[:10]} "
                        f"(same path, different model)"),
                    'contradicted_by_id': contradicted_by.get('id'),
                }
                p.update(_patch)
                self._persist_delta(p.get('id', ''), _patch)
                updated.append(p)

        if updated:
            # v4.13.37 / v4.14.3.13: deltas are written inline at the
            # mutation sites above (per-record via _persist_delta).
            # The diagnostic log below stays useful — it still tracks
            # file-size growth around the write window (deltas grow
            # the file by ~200 bytes each) and reads disk back to
            # verify the new statuses are visible via PredictionsLog
            # semantics. The "did not stick" warning now reflects
            # delta-merge correctness rather than full-rewrite
            # success.
            try:
                from pathlib import Path as _P37
                log_path = _P37(self.path).parent / "v4.13.37_closer.log"
                size_before = self.path.stat().st_size if self.path.exists() else 0
                with open(log_path, 'a', encoding='utf-8') as lf:
                    lf.write(
                        f"[{datetime.now().isoformat()}] "
                        f"check_supersessions: {len(updated)} updates "
                        f"({sum(1 for p in updated if p.get('status')==OUTCOME_SUPERSEDED)} superseded, "
                        f"{sum(1 for p in updated if p.get('status')==OUTCOME_CONTRADICTED)} contradicted)\n")
                    lf.write(f"  cache size: {len(self._cache)} entries\n")
                    lf.write(
                        f"  file size before delta writes: "
                        f"{size_before} bytes\n")

                # v4.14.3.13: no _persist_full call here — deltas
                # already written per-record above. Recompute file
                # size for the post-write diagnostic.

                size_after = self.path.stat().st_size if self.path.exists() else 0
                # Sanity check: read disk back via the same merge
                # logic the live reader uses, count statuses.
                disk_super = 0
                disk_contra = 0
                disk_total = 0
                try:
                    merged, _ = self._load_with_counts()
                    disk_total = len(merged)
                    for e in merged:
                        if e.get('status') == OUTCOME_SUPERSEDED:
                            disk_super += 1
                        elif e.get('status') == OUTCOME_CONTRADICTED:
                            disk_contra += 1
                except Exception:
                    pass
                with open(log_path, 'a', encoding='utf-8') as lf:
                    lf.write(
                        f"  file size after delta writes:  "
                        f"{size_after} bytes\n")
                    lf.write(
                        f"  disk read-back (merged): {disk_total} "
                        f"entries, {disk_super} superseded, "
                        f"{disk_contra} contradicted\n")
                    if disk_super == 0 and disk_contra == 0 and len(updated) > 0:
                        lf.write(
                            f"  *** WARNING: persisted 0 superseded/"
                            f"contradicted on disk despite "
                            f"{len(updated)} in memory. Delta writes "
                            f"may have failed (check stderr for "
                            f"[PredictionsLog] amber lines). ***\n")
                    lf.write("\n")
            except Exception as _e:
                # Never let logging break the closer. v4.14.3.13: no
                # fallback _persist_full needed since deltas were
                # written inline at the mutation sites.
                try:
                    print(
                        f"[PredictionsLog] check_supersessions "
                        f"diagnostic-log failed: "
                        f"{type(_e).__name__}: {_e}")
                except Exception:
                    pass
        return updated

    def mark_position_sold(self, ticker: str, sold_price: float,
                            closed_at: str | None = None,
                            before_ts: datetime | None = None,
                            ) -> list[dict]:
        """When the user sells a position, close all open BUY predictions
        for that ticker as 'sold'. Returns updated entries.

        v4.14.3.13: writes per-record deltas instead of a single
        full-file rewrite. Pre-v4.14.3.13 this method ALSO contained
        a dormant deadlock — it held self._lock while calling
        _persist_full which tried to acquire the same non-reentrant
        Lock. Never triggered because mark_position_sold is a rare
        user action. v4.14.3.13 switched self._lock to RLock for
        reentrance safety; delta writes still work correctly under
        the held lock.

        v4.14.5.14-sold-prediction-tracking: optional `closed_at`
        (ISO string) and `before_ts` (datetime). Both default None →
        byte-identical legacy behaviour (closed_at=now, no time
        filter). They exist for the one-shot backfill of past sales:
        `closed_at` stamps the historical sell date, and `before_ts`
        restricts the close to predictions made AT OR BEFORE the sale
        (so a BUY re-recommended AFTER you sold isn't retroactively
        closed by that old sale). Unparseable prediction timestamps
        are INCLUDED (legacy behaviour preserved)."""
        ticker = ticker.upper()
        updated = []
        with self._lock:
            for p in self._cache:
                if (p.get('status') == OUTCOME_OPEN
                        and p.get('ticker', '').upper() == ticker):
                    if before_ts is not None:
                        try:
                            _pts = datetime.fromisoformat(
                                p.get('timestamp', ''))
                            if _pts > before_ts:
                                continue
                        except Exception:
                            pass  # unparseable → include (legacy)
                    _patch = {
                        'status': OUTCOME_SOLD,
                        'closed_at': closed_at or datetime.now().isoformat(),
                        'close_price': sold_price,
                        'notes': f"position sold at ${sold_price:g}",
                    }
                    p.update(_patch)
                    self._persist_delta(p.get('id', ''), _patch)
                    updated.append(dict(p))
        return updated

    def backfill_sold_from_closed(self,
                                  closed_trades: list[dict],
                                  ) -> tuple[int, int]:
        """v4.14.5.14-sold-prediction-tracking (Fix C): one-shot backfill.
        Given the Holdings manager's `closed` trade list (each with
        ticker / sell_price / sell_date), mark any still-OPEN BUY
        predictions for those tickers as 'sold' at the recorded sale
        price and date. Uses the EXACT historical sell price (no market
        approximation) because the closed list stores it. Idempotent at
        the data level: a second run finds nothing still 'open' to mark.

        Returns (n_trades_with_matches, n_predictions_marked). Never
        raises — skips any malformed trade row."""
        n_trades = 0
        n_marked = 0
        for c in (closed_trades or []):
            try:
                tk = (c.get('ticker') or '').strip().upper()
                sp = c.get('sell_price')
                if not tk or sp in (None, ''):
                    continue
                sp = float(sp)
                sd = c.get('sell_date') or c.get('closed_at') or None
                before_ts = None
                if sd:
                    try:
                        before_ts = datetime.fromisoformat(sd)
                    except Exception:
                        before_ts = None
                upd = self.mark_position_sold(
                    tk, sp, closed_at=sd, before_ts=before_ts)
                if upd:
                    n_trades += 1
                    n_marked += len(upd)
            except Exception:
                continue
        return n_trades, n_marked

    # ─── Stats ───

    def aggregate_stats(self,
                         ticker: str | None = None,
                         path: str | None = None,
                         direction: str | None = None,
                         confidence: str | None = None,
                         days_back: int | None = None,
                         ) -> dict:
        """Aggregate accuracy stats over filtered predictions.
        Filters are AND-combined."""
        cutoff = None
        if days_back:
            cutoff = datetime.now() - timedelta(days=days_back)

        with self._lock:
            preds = list(self._cache)

        # Apply filters
        if ticker:
            tu = ticker.upper()
            preds = [p for p in preds if p.get('ticker', '').upper() == tu]
        if path:
            preds = [p for p in preds if p.get('path') == path]
        if direction:
            preds = [p for p in preds
                     if (p.get('direction') or '').upper() == direction.upper()]
        if confidence:
            preds = [p for p in preds
                     if (p.get('confidence') or '').upper() == confidence.upper()]
        if cutoff:
            kept = []
            for p in preds:
                try:
                    ts = datetime.fromisoformat(p.get('timestamp', ''))
                    if ts >= cutoff:
                        kept.append(p)
                except Exception:
                    continue
            preds = kept

        return self._compute_stats_dict(preds)

    def _compute_stats_dict(self, preds: list[dict]) -> dict:
        """Helper: turn a filtered prediction list into a stats dict.
        Same return shape as aggregate_stats. Factored out so
        compute_all_stats can re-use it without re-filtering."""
        total = len(preds)
        n_closed = 0
        n_target = 0
        n_stop = 0
        n_expired = 0
        n_sold = 0
        # v4.14.5.14-canonical-accuracy-definition: count retractions so
        # the (super+contra) noise is surfaced as its own metric instead
        # of silently inflating the accuracy denominator.
        n_super = 0
        n_contra = 0
        # v4.14.5.14-sold-prediction-tracking: classify manual sells by
        # realized P/L (exit vs entry). These are ADDITIVE — target_hit /
        # stop_hit / target_rate_pct keep their exact prior meaning (the
        # market-decided auto-detected accuracy), so nothing downstream
        # that reads them changes. The realized_* rollups below combine
        # auto-detected target/stop wins with your manual-sell wins so the
        # Track Record can show a win count that matches your real trades.
        n_sold_win = 0
        n_sold_loss = 0
        n_sold_flat = 0
        # v4.14.5.14-hold-grading: HOLD verdicts tracked on their OWN axis,
        # never mixed into the BUY target/stop accuracy denominator.
        n_hold_held = 0
        n_hold_broken = 0
        # v4.14.5.14-trim-buy-more-grading: TRIM and BUY MORE verdict axes,
        # each isolated from the BUY target/stop denominator (same as HOLD).
        n_trim_correct = 0
        n_trim_incorrect = 0
        n_buy_more_correct = 0
        n_buy_more_incorrect = 0
        # v4.14.5.62-validated-accuracy: shared attribution gate. No-op when
        # the use_validated_accuracy flag is off (byte-identical). When on,
        # every count below reflects only tier-2-validated app picks, so the
        # realized rollup + aggregate stats agree with the headline accuracy.
        try:
            import tm_source_accuracy as _tsa_attr
        except Exception:
            _tsa_attr = None
        for p in preds:
            if _tsa_attr is not None and not _tsa_attr.is_attributable(p):
                continue
            status = p.get('status')
            if status == OUTCOME_OPEN:
                continue
            n_closed += 1
            if status == OUTCOME_TARGET_HIT:
                n_target += 1
            elif status == OUTCOME_STOP_HIT:
                n_stop += 1
            elif status == OUTCOME_EXPIRED:
                n_expired += 1
            elif status == OUTCOME_SOLD:
                n_sold += 1
                _cls = _classify_sold_outcome(p)
                if _cls == 'win':
                    n_sold_win += 1
                elif _cls == 'loss':
                    n_sold_loss += 1
                else:
                    n_sold_flat += 1
            elif status == OUTCOME_SUPERSEDED:
                n_super += 1
            elif status == OUTCOME_CONTRADICTED:
                n_contra += 1
            elif status == OUTCOME_HOLD_HELD:
                n_hold_held += 1
            elif status == OUTCOME_HOLD_BROKEN:
                n_hold_broken += 1
            elif status == OUTCOME_TRIM_CORRECT:
                n_trim_correct += 1
            elif status == OUTCOME_TRIM_INCORRECT:
                n_trim_incorrect += 1
            elif status == OUTCOME_BUY_MORE_CORRECT:
                n_buy_more_correct += 1
            elif status == OUTCOME_BUY_MORE_INCORRECT:
                n_buy_more_incorrect += 1

        # v4.14.5.14-canonical-accuracy-definition: accuracy denominator
        # is DECIDED predictions only (target_hit + stop_hit). superseded
        # / contradicted / expired / sold / cancelled are non-verdicts and
        # excluded — matching tm_source_accuracy + compute_path_track_stats.
        n_decided = n_target + n_stop
        # v4.14.5.14-hold-grading: HOLD verdicts are their own decided cohort;
        # subtract them so they don't masquerade as BUY non-verdict noise.
        n_hold_decided = n_hold_held + n_hold_broken
        hold_accuracy = ((n_hold_held / n_hold_decided * 100)
                         if n_hold_decided else 0.0)
        # v4.14.5.14-trim-buy-more-grading: TRIM / BUY MORE decided cohorts +
        # accuracy, each on its own axis (None when no data, so the display
        # can hide the line — never "0 of 0"). Subtracted from n_non_decided
        # below so they don't masquerade as BUY non-verdict noise (same
        # isolation HOLD gets).
        n_trim_decided = n_trim_correct + n_trim_incorrect
        trim_accuracy = ((n_trim_correct / n_trim_decided * 100)
                         if n_trim_decided else None)
        n_buy_more_decided = n_buy_more_correct + n_buy_more_incorrect
        buy_more_accuracy = ((n_buy_more_correct / n_buy_more_decided * 100)
                             if n_buy_more_decided else None)
        n_non_decided = (n_closed - n_decided - n_sold
                         - n_hold_decided
                         - n_trim_decided - n_buy_more_decided)
        # ^ super+contra+expired+cancelled (HOLD/TRIM/BUY MORE excluded)
        target_rate = (n_target / n_decided * 100) if n_decided else 0.0
        stop_rate = (n_stop / n_decided * 100) if n_decided else 0.0
        retract_rate = ((n_super + n_contra) / n_closed * 100) if n_closed else 0.0

        realized_wins = n_target + n_sold_win
        realized_losses = n_stop + n_sold_loss
        realized_decided = realized_wins + realized_losses
        realized_win_rate = (100.0 * realized_wins / realized_decided
                             if realized_decided else 0.0)

        return {
            'total': total,
            'open': total - n_closed,
            'closed': n_closed,
            'target_hit': n_target,
            'stop_hit': n_stop,
            'expired': n_expired,
            'sold': n_sold,
            # v4.14.5.14-canonical-accuracy-definition: these two are now
            # target/(target+stop) — the decided cohort only. 'closed'
            # above stays as the informational "total resolved" count.
            'target_rate_pct': target_rate,
            'stop_rate_pct': stop_rate,
            'n_decided': n_decided,             # target_hit + stop_hit
            'n_non_decided': n_non_decided,     # super+contra+expired+cancelled
            'superseded': n_super,
            'contradicted': n_contra,
            'retract_rate_pct': retract_rate,   # (super+contra)/closed
            # sample-size warning now keyed on DECIDED count (the rate's
            # actual denominator), not all closures.
            'sample_size_warning': n_decided < 20,
            # v4.14.5.14-sold-prediction-tracking additive fields:
            'sold_win': n_sold_win,
            'sold_loss': n_sold_loss,
            'sold_flat': n_sold_flat,
            'realized_wins': realized_wins,
            'realized_losses': realized_losses,
            'realized_decided': realized_decided,
            'realized_win_rate_pct': realized_win_rate,
            # v4.14.5.14-hold-grading: separate HOLD verdict track (band held
            # vs band broken). NOT part of the BUY target/stop denominator.
            'hold_held': n_hold_held,
            'hold_broken': n_hold_broken,
            'hold_decided': n_hold_decided,
            'hold_accuracy_pct': hold_accuracy,
            # v4.14.5.14-trim-buy-more-grading: separate TRIM / BUY MORE verdict
            # tracks. accuracy_pct is None (not 0.0) when nothing's decided, so
            # the Track Record line can stay hidden until real data exists.
            'trim_correct': n_trim_correct,
            'trim_incorrect': n_trim_incorrect,
            'trim_decided': n_trim_decided,
            'trim_accuracy_pct': trim_accuracy,
            'buy_more_correct': n_buy_more_correct,
            'buy_more_incorrect': n_buy_more_incorrect,
            'buy_more_decided': n_buy_more_decided,
            'buy_more_accuracy_pct': buy_more_accuracy,
        }

    def compute_all_stats(self) -> dict:
        """v4.13.61: ONE-PASS computation of all the rollups Track
        Record needs. Replaces 8+ separate aggregate_stats() calls
        that each iterate the full predictions cache.

        Returns a dict with:
            'overall': stats dict (all predictions)
            'by_path': {path_name: stats_dict, ...}
            'by_confidence': {conf_name: stats_dict, ...}

        On a populated install with thousands of predictions this
        drops Track Record load time from ~30s to ~3s.
        """
        with self._lock:
            preds = list(self._cache)

        # Group predictions ONCE
        by_path: dict[str, list[dict]] = {}
        by_conf: dict[str, list[dict]] = {}
        for p in preds:
            path = p.get('path') or ''
            if path:
                by_path.setdefault(path, []).append(p)
            conf = (p.get('confidence') or '').upper()
            if conf:
                by_conf.setdefault(conf, []).append(p)

        # Compute stats for each group from the pre-grouped lists.
        # Each call to _compute_stats_dict is just a count loop, no
        # filtering — much faster than re-scanning the full preds list.
        return {
            'overall': self._compute_stats_dict(preds),
            'by_path': {path: self._compute_stats_dict(plist)
                         for path, plist in by_path.items()},
            'by_confidence': {conf: self._compute_stats_dict(plist)
                                for conf, plist in by_conf.items()},
        }



# ═════════════════════════════════════════════════════════════════════════════
# v4.10.0 — SCAN SNAPSHOTS
# ═════════════════════════════════════════════════════════════════════════════
# Goal: when filter_candidates picks ~15 tickers and downloads quotes/data
# for them, save the WHOLE snapshot (not just the resulting predictions) so
# we can answer "what did the data look like at scan time" later. Foundation
# for v4.10.1 unified scan that re-uses fresh snapshots without re-fetching.
#
# v4.10.0 only WRITES snapshots. Reading them and re-using is v4.10.1.
# If anything goes wrong with the save, it's caught and ignored — the scan
# itself is unaffected. This is the conservative approach: foundation laid,
# zero behavior change.

class ScanSnapshot:
    """A frozen capture of a scan's input data.

    Stores: scan_id, timestamps (overall + per-ticker), candidates list,
    full quote/news data per candidate, source attribution (Yahoo/Stooq).

    Files live at data/snapshots/scan_YYYYMMDD_HHMMSS.json. Filename uses
    underscores (no colons) for Windows compatibility. Each snapshot is
    self-contained — no cross-references needed to interpret it.

    v4.10.0 — write-only foundation. Reading + re-use comes in v4.10.1.
    """

    SCHEMA_VERSION = 1  # bump if format changes incompatibly

    def __init__(self, snapshots_dir: Path):
        self.snapshots_dir = Path(snapshots_dir)
        try:
            self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def save(self,
             scan_id: str,
             candidates: list[dict],
             universe_source: str | None = None,
             path: str | None = None,
             extras: dict | None = None) -> str | None:
        """Save a scan snapshot. Returns the file path on success, None on
        failure (failure is silent — never raises, never blocks).

        scan_id: e.g. 'scan_20260428_114500' or 'consensus_20260427_205915'
        candidates: list of candidate dicts as returned from filter_candidates
                    (each has ticker, price, change_pct, volume, etc.)
        universe_source: e.g. 'iwm', 'iwv' — which universe was scanned
        path: e.g. 'aggressive', 'moderate', 'conservative'
        extras: anything else worth preserving (per-ticker history snapshots,
                news features, etc.). Optional.
        """
        try:
            # Build the snapshot dict
            now_iso = datetime.now().isoformat(timespec='seconds')
            snapshot = {
                'schema_version': self.SCHEMA_VERSION,
                'scan_id': scan_id,
                'created_at': now_iso,
                'universe_source': universe_source,
                'path': path,
                'candidate_count': len(candidates),
                'candidates': [],
            }

            # Capture each candidate's data with a per-ticker timestamp
            # (for v4.10's snapshot-freshness logic — different tickers may
            # be slightly stale at different rates, especially during
            # market hours)
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                entry = {
                    'ticker': c.get('ticker'),
                    'price': c.get('price'),
                    'change_pct': c.get('change_pct'),
                    'volume': c.get('volume'),
                    'prev_close': c.get('prev_close'),
                    # v4.9.4: source field tells us which pipe served this
                    # data (Yahoo, Stooq, etc.)
                    'source': c.get('source'),
                    # Per-ticker capture timestamp; defaults to overall
                    'captured_at': c.get('captured_at') or now_iso,
                }
                # Preserve any other keys we don't explicitly know about
                for k, v in c.items():
                    if k not in entry and k not in (
                            'history', 'technicals'):  # skip heavy nested
                        # Only preserve JSON-serializable scalars/lists/dicts
                        try:
                            json.dumps(v, default=str)
                            entry[k] = v
                        except Exception:
                            pass
                snapshot['candidates'].append(entry)

            if extras and isinstance(extras, dict):
                snapshot['extras'] = extras

            # Persist atomically: write to .tmp, then rename
            file_path = self.snapshots_dir / f"{scan_id}.json"
            tmp_path = file_path.with_suffix('.json.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(snapshot, f, indent=2, default=str)
            tmp_path.replace(file_path)

            return str(file_path)
        except Exception:
            # Never let snapshot write fail the actual scan
            return None

    def list_snapshots(self) -> list[dict]:
        """Return metadata for all stored snapshots, newest first.
        Each entry has: scan_id, created_at, candidate_count, file_path,
        size_bytes. Reading the full snapshot requires .load().
        """
        out = []
        try:
            for p in sorted(
                    self.snapshots_dir.glob('*.json'),
                    key=lambda x: x.stat().st_mtime,
                    reverse=True):
                if p.suffix == '.tmp':
                    continue
                try:
                    with open(p, 'r', encoding='utf-8') as f:
                        d = json.load(f)
                    out.append({
                        'scan_id': d.get('scan_id', p.stem),
                        'created_at': d.get('created_at', ''),
                        'candidate_count': d.get('candidate_count', 0),
                        'universe_source': d.get('universe_source'),
                        'path': d.get('path'),
                        'file_path': str(p),
                        'size_bytes': p.stat().st_size,
                    })
                except Exception:
                    # Malformed snapshot — show file but mark unreadable
                    out.append({
                        'scan_id': p.stem,
                        'created_at': '',
                        'candidate_count': 0,
                        'universe_source': None,
                        'path': None,
                        'file_path': str(p),
                        'size_bytes': p.stat().st_size,
                        'unreadable': True,
                    })
        except Exception:
            pass
        return out

    def load(self, scan_id: str) -> dict | None:
        """Load a saved snapshot by scan_id. Returns None if not found
        or unreadable. v4.10.1 will use this to re-run AI against saved
        snapshots without re-fetching."""
        try:
            file_path = self.snapshots_dir / f"{scan_id}.json"
            if not file_path.exists():
                return None
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None


# Module-level convenience: a single ScanSnapshot instance gets installed
# at app startup (set_scan_snapshot_path). filter_candidates uses it on
# every successful run to write a snapshot. Optional — if not set, no
# snapshot gets written and the scan still works exactly the same.
_SCAN_SNAPSHOT: ScanSnapshot | None = None


def set_scan_snapshot_path(snapshots_dir: Path) -> None:
    """Install the global ScanSnapshot instance. Called once at startup."""
    global _SCAN_SNAPSHOT
    try:
        _SCAN_SNAPSHOT = ScanSnapshot(snapshots_dir)
    except Exception:
        _SCAN_SNAPSHOT = None


def get_scan_snapshot() -> ScanSnapshot | None:
    """Return the installed ScanSnapshot instance, if any."""
    return _SCAN_SNAPSHOT


# ─── Helpers for prompt-side integration ──────────────────────────────

def _format_prediction_request_block(direction_options: str,
                                     verdict_first: bool = False) -> str:
    """v4.14.2 stage 4: shared body. Owned + candidate variants below
    differ only in the direction vocabulary they offer.

    v4.14.6.7-verdict-parse-and-schema (2026-06-11): `verdict_first` is
    an opt-in flag the candidate variant uses to flip the structured
    summary to the FRONT of the response. Default False keeps the
    owned-position prompt byte-identical (structured-summary-at-end,
    same as v4.14.6.6 and prior). With True, the instruction asks the
    model to BEGIN with the structured summary so DIRECTION survives
    any output-token-cap truncation on verbose analyses. The schema
    fields and labels are unchanged either way.
    """
    if verdict_first:
        return f"""\
BEGIN your response with this structured summary (do NOT put it at the
end of your response). Use these labels verbatim so the app can parse
them; then write your analysis below it.

DIRECTION: BUY  ← {direction_options}
BUY_ZONE: $X.XX - $X.XX  ← (price range to consider entering; omit if AVOID)
TARGET: $X.XX  ← (price level you think it could reach if thesis plays out)
STOP_LOSS: $X.XX  ← (price where the thesis breaks; cut losses)
TIMEFRAME: N days  ← (or N weeks; how long for thesis to play out)
CONFIDENCE: LOW  ← (or MODERATE or HIGH; be honest — most calls should be LOW or MODERATE)

Then write your analysis. Do not invent confidence you don't have. HIGH
confidence requires multiple strong signals aligning (technicals + news
catalyst + sector momentum). LOW is the appropriate default for
speculative situations.
"""
    return f"""\
At the END of your response, include this structured summary in this EXACT format
(use these labels verbatim so the app can parse them):

DIRECTION: BUY  ← {direction_options}
BUY_ZONE: $X.XX - $X.XX  ← (price range to consider entering; omit if AVOID)
TARGET: $X.XX  ← (price level you think it could reach if thesis plays out)
STOP_LOSS: $X.XX  ← (price where the thesis breaks; cut losses)
TIMEFRAME: N days  ← (or N weeks; how long for thesis to play out)
CONFIDENCE: LOW  ← (or MODERATE or HIGH; be honest — most calls should be LOW or MODERATE)

Do not invent confidence you don't have. HIGH confidence requires multiple
strong signals aligning (technicals + news catalyst + sector momentum).
LOW is the appropriate default for speculative situations.
"""


def format_prediction_request_block_owned() -> str:
    """v4.14.2 stage 4: owned-position prompt (build_holding_analysis).
    HOLD makes sense when there IS a position to hold."""
    return _format_prediction_request_block("(or HOLD, or AVOID)")


def format_prediction_request_block_candidate() -> str:
    """v4.14.2 stage 4: candidate prompt (build_candidate_prompt,
    fresh-buy consensus). HOLD is meaningless on tickers the user doesn't
    own — replaced with WATCH (semantically: 'interesting, wait for
    better entry').

    v4.14.6.6-tier1-singlepath-prompt (2026-06-11): prepend a one-line
    REQUIRED instruction. Small/fast Tier-1 models (Groq's llama-3.1-
    8b, mixtral) sometimes burn their output token budget on prose
    before reaching the structured summary at the end — leaving the
    parser with no DIRECTION line to extract. The REQUIRED prefix
    primes the model to treat the structured fields as non-negotiable,
    even at the cost of shortening the analysis. Scoped to the
    candidate path (Tier-1 scan + fresh-buy consensus); the owned
    path (build_holding_analysis) is unchanged.

    v4.14.6.7-verdict-parse-and-schema (2026-06-11): also flip the
    structured-summary instruction from "at the END" to "at the
    BEGINNING" via verdict_first=True. Resolves the contradiction
    in v4.14.6.6 (REQUIRED + put-it-last) and makes DIRECTION
    survive any output-token-cap truncation. Candidate-scoped; the
    owned-position prompt is unchanged.
    """
    required_line = (
        "REQUIRED — your response MUST include the DIRECTION line "
        "below (and the rest of the structured summary). If you must "
        "trade off length, shorten the analysis — never the verdict.\n"
        "\n")
    return required_line + _format_prediction_request_block(
        "(or WATCH, or AVOID)", verdict_first=True)


# ─── v4.14.5.14c (Patch 1/2): one-ticker-ALL-paths request block ─────
# Structural foundation only — these two pure functions are shipped
# callable-but-UNWIRED (same discipline as v4.14.5.0's filter). The
# dispatch/writer fan-out that actually calls them is the separate
# follow-up patch, behind cfg['use_unified_multi_path_prompts']
# (default False). Schema is deliberately path-AGNOSTIC: it carries
# the internal path KEY (e.g. 'slow_safe') in the delimiter, never
# price/pace semantics — so the deferred paths-as-pace migration can
# rewrite each path's goal-line text later WITHOUT touching this
# structure, parser, or the eventual writer fan-out.
#
# Delimiter choice = structured text `=== PATH: <key> ===`, NOT JSON.
# Reasoning (NOT a live test — live model-reliability is the user's
# flag-on validation in Patch 2; this environment has no network /
# live models): structured text degrades gracefully — a model that
# drops or mangles one block still yields the others, and the parser
# reuses the existing tolerant single-block `parse_prediction` per
# segment. A single malformed JSON array would lose ALL paths at
# once. Forgiving > clean here because the weakest models
# (Llama-3.1-8b via Groq/Cerebras) are the risk.

_MULTI_PATH_HEADER_RE = re.compile(
    r'^[ \t]*=+[ \t]*PATH[ \t]*:[ \t]*([A-Za-z0-9_]+)[ \t]*=*[ \t]*$',
    re.IGNORECASE | re.MULTILINE)


def format_multi_path_prediction_request_block(paths: list) -> str:
    """Build the multi-path structured-output instruction for a
    one-ticker-all-paths call. `paths` = the internal path keys the
    ticker is ELIGIBLE for (caller computes eligibility exactly as
    today via pool ∩ price-band gate; this function is pure text).

    The single shared analysis lives in the prompt body the caller
    already assembles; this block only specifies the per-path verdict
    output. If `paths` has one entry the output is one block — i.e.
    it degenerates to today's single-path shape, just with a header.

    Pure / no side effects / never raises (empty/!list → '')."""
    if isinstance(paths, str) or not isinstance(paths, (list, tuple)):
        return ''  # a bare string / non-sequence is not a path list
    try:
        keys = [str(p).strip() for p in (paths or []) if str(p).strip()]
    except Exception:
        return ''
    if not keys:
        return ''
    body = _format_prediction_request_block("(or WATCH, or AVOID)")
    lines = [
        "You are evaluating this ONE ticker for MULTIPLE strategy "
        "paths, all from the SAME analysis above. The fundamentals / "
        "technicals / news do not change between paths — only how "
        "strict the entry bar is for each path's goal.",
        "",
        f"Output EXACTLY one verdict block for EACH of these "
        f"{len(keys)} path(s), each introduced by its header line "
        f"VERBATIM (the app parses on these headers):",
        "",
    ]
    for k in keys:
        lines.append(f"=== PATH: {k} ===")
        lines.append("<the structured summary for this path, in the "
                      "exact field format described below>")
        lines.append("")
    lines.append("Field format for EACH path's block "
                  "(use these labels verbatim):")
    lines.append("")
    lines.append(body)
    lines.append("")
    lines.append("Do NOT merge paths. Do NOT omit a path. If a path "
                 "is a clear AVOID, still emit its block with "
                 "DIRECTION: AVOID. Reproduce each path's header line "
                 "exactly as shown above.")
    return "\n".join(lines)


def parse_multi_path_prediction(text: str, ticker: str,
                                 paths: list,
                                 current_price: float | None = None
                                 ) -> dict:
    """Parse a one-ticker-all-paths response into {path_key: pred}.

    Splits `text` on the `=== PATH: <key> ===` headers and runs the
    EXISTING tolerant single-block `parse_prediction` on each segment
    (no reinvention). FAIL-OPEN by contract:
      - no headers found at all  → {}  (caller falls back to legacy
        per-path dispatch; never crash)
      - some expected paths missing/garbled → only the ones that
        parsed a real DIRECTION are returned; missing ones are simply
        absent (caller logs + can re-dispatch them legacy)
      - never raises.

    Only keys in `paths` (the eligible set) are accepted, so a model
    hallucinating an extra `=== PATH: foo ===` can't inject a bogus
    record. Matching is case-insensitive on the key."""
    out: dict = {}
    try:
        want = {str(p).strip().lower(): str(p).strip()
                for p in (paths or []) if str(p).strip()}
        if not text or not want:
            return {}
        matches = list(_MULTI_PATH_HEADER_RE.finditer(text))
        if not matches:
            return {}
        for i, m in enumerate(matches):
            key_raw = (m.group(1) or '').strip().lower()
            if key_raw not in want:
                continue  # not an eligible path → ignore (anti-hallucination)
            seg_start = m.end()
            seg_end = (matches[i + 1].start()
                       if i + 1 < len(matches) else len(text))
            segment = text[seg_start:seg_end]
            try:
                pred = parse_prediction(segment, ticker,
                                        current_price=current_price)
            except Exception:
                continue
            if pred and pred.get('direction'):
                out[want[key_raw]] = pred
    except Exception:
        return out
    return out


def format_prediction_request_block() -> str:
    """v4.14.2 stage 4: legacy compatibility alias. New call sites
    should pick format_prediction_request_block_owned() or
    format_prediction_request_block_candidate() based on context.
    Kept here so any unmigrated caller keeps working — emits the
    owned-style vocabulary (HOLD valid) which matches the pre-stage-4
    behavior for both prompt kinds."""
    return format_prediction_request_block_owned()


def format_track_record_context(prediction_log: PredictionsLog,
                                 path: str | None = None,
                                 ticker: str | None = None) -> str:
    """Returns a short prompt block summarizing context for the AI.

    v4.13.1 fix: distinguishes between (a) the AI's own paper-trade
    predictions (which are tracked automatically and can have
    misleading "stop_hit" rates from intra-day price moves) and
    (b) the user's actual realized buy/sell trades.

    Pre-v4.13.1, this function reported AI paper-trade stop-hit
    counts as "your track record," which the models read as
    "the user loses every trade" and biased toward AVOID. That was
    wrong. The user's real track record is in portfolio.json closed[].

    Now: only emit a track-record line when there are >= 5 closed
    AI predictions (signal vs noise floor), and FRAME it correctly
    as paper-trade self-evaluation, not the user's real performance.
    Suppress entirely when the data would mislead more than inform.
    """
    overall = prediction_log.aggregate_stats(days_back=180)
    closed_n = overall.get('closed', 0)

    # Below the noise floor: say nothing rather than mislead.
    if closed_n < 5:
        return ""

    # NEVER claim this is "your" or "the user's" track record.
    # These are auto-tracked paper predictions, not real trades.
    lines = ["AI PREDICTION TRACKING (paper-trade self-eval, not real trades):"]
    # v4.14.5.14-canonical-accuracy-definition: hit rate is target/(target+
    # stop) — the DECIDED cohort only. Retractions/expiries are non-verdicts
    # and reported separately so the AI isn't told a falsely-low accuracy.
    n_decided = overall.get('n_decided', 0)
    lines.append(f"- {closed_n} AI predictions resolved in last 180 days; "
                  f"{n_decided} reached a market verdict (target or stop)")

    target_pct = overall.get('target_rate_pct', 0)
    stop_pct = overall.get('stop_rate_pct', 0)
    if n_decided:
        lines.append(f"  - hit rate: target {overall.get('target_hit', 0)} "
                      f"({target_pct:.0f}%) vs stop {overall.get('stop_hit', 0)} "
                      f"({stop_pct:.0f}%) of {n_decided} decided; "
                      f"expired without verdict: {overall.get('expired', 0)}")
    _retr = overall.get('superseded', 0) + overall.get('contradicted', 0)
    if _retr:
        lines.append(f"  - retraction rate: "
                      f"{overall.get('retract_rate_pct', 0):.0f}% "
                      f"({_retr} predictions superseded/contradicted by "
                      f"re-analysis before price could rule — these do NOT "
                      f"count against the hit rate)")
    lines.append("- NOTE: stop_hit on a paper prediction means price "
                  "*touched* stop intraday; many of those would have "
                  "recovered. Do NOT read this as the user losing trades.")

    if ticker:
        ticker_recent = prediction_log.get_recent_for_ticker(ticker, limit=3)
        if ticker_recent:
            lines.append(f"- Recent paper predictions for {ticker}:")
            for p in ticker_recent:
                d = p.get('direction', '?')
                t = p.get('target')
                s = p.get('status', '?')
                ts_short = p.get('timestamp', '')[:10]
                t_str = f"${t}" if t else "no-target"
                lines.append(f"  - {ts_short}: {d} {t_str} → {s}")

    if overall.get('sample_size_warning'):
        lines.append("- Sample size small; treat percentages as noisy.")

    lines.append("Evaluate this candidate on its own merits. The AI "
                  "tracking above is informational only.")
    return "\n".join(lines)
