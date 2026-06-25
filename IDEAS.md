# Tired Market — Ideas / Backlog

Candidate improvements not yet scheduled. Each entry notes priority and scope.

---

## [v110 candidate] Split universe seed from live cache

**Priority:** Low (quality-of-life, not user-facing)

**Problem:** `data/universe_iwv.json` plays two roles under one filename — the
bundled first-run seed (read-only, shipped in the exe) and the live SEC/IWV
fetch cache (rewritten on refresh by the fundfile daemon). It's force-tracked in
git (`.gitignore` line 42 `!data/universe_iwv.json`), so in a dev checkout every
universe refresh rewrites the tracked file and dirties the working tree
permanently. This perpetual dirty state obscures what's actually committed vs.
just a cache refresh — a contributing factor to source/GitHub drift.

**Fix:** Separate the roles.
- Ship the immutable seed under a distinct name, e.g. `data/universe_iwv.seed.json`
  — force-tracked, bundled in the spec, never written by the fetcher.
- Let the live `data/universe_iwv.json` cache be gitignored (drop the line-42
  re-allow).
- Point `_bundled_snapshot_tickers` fallback at the `.seed.json` name.

**Payoff:** Dev runs stop dirtying the tree; the seed stays a known-good snapshot;
the "what's committed" signal stays clean. Out of scope for the 109 bugfix release.
