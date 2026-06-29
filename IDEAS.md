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

---

## [Decided / Implemented] Disk probe (Get-PhysicalDisk) removed from startup

**Status:** Done (code-only, v4.14.6.109; not yet built/released).

Disk probe (Get-PhysicalDisk PowerShell) removed from startup.
- Cost ~2.04s fixed; returned 'unknown' on the dev rig anyway.
- Only fed `_classify_hw_tier` -> tier -> `_fill_defer_ms` (when a background
  fill starts). No correctness/data/UI depended on it.
- On a real potato RAM/cores already force 'low' before the disk clause, so the
  probe never helped the weak target it was meant to protect.
- Sole config it uniquely served: good-CPU-on-spinning-HDD (looks capable by
  RAM/cores but has slow storage) — loses a longer fill-defer window; fill still
  runs, no break.
- `_detect_disk_type()` left defined but uncalled. If the HDD case ever matters,
  a self-measuring replacement (time the app's own cache.db read/writes, taps
  #1/#2) is the better path — build map in DISK_TIER_FINDINGS.md Part 3.

Prevents re-deriving "is the disk probe worth it?" later. Measured before/after:
`disk_probe` startup lap dropped ~2037ms -> ~0.3ms; `[hardware] tier=normal` log
and behavior unchanged (see STARTUP_TIMING_FINDINGS.md / DISK_TIER_FINDINGS.md).

---

## [Decided / Implemented] Tier-2 / consensus model routing — DO NOT re-pin

**Status:** Done (code-only, v4.14.6.111; not yet built/released). See
TIER2_ROUTING_FINDINGS.md + ROTATION_DISCOVERY_FINDINGS.md for the full trace.

This tier-2 model decision has flip-flopped before — v4.14.5.71 moved Groq's
tier-2 slot to 8B (to stop the 70B per-minute cap tripping); v4.14.6.71 reverted
it to 70B (to stop Gemini-flash RPM panel starvation → empty boards). It must not
bounce again. The current design resolves BOTH pressures structurally:

- **Tier-2 no longer pins a model.** It rotates the provider's full `models[]`
  (flat round-robin + per-model cooldown-skip), so no single model is hit every
  pass and a rate-limited model is skipped, not dropped. (tm_layer2_validation.py
  `_select_tier2_providers` / `_select_tier2_providers_flexible` — they keep the
  provider's `models[]` instead of collapsing to `copy['models']=[model]`.)
- **Consensus rotates CAPABLE-FIRST with graceful descent.** It orders the panel
  most-capable → least and descends (cooldown-skip + `_try_backfill_substitute`)
  when a top model is cooled, never collapsing to one pinned model.
  (tm_consensus.py, `ordered_models` sorted by `model_capability_rank`.)
- **The empty-board / panel-starvation risk that once justified pinning 70B is
  now handled by rotation's cooldown-skip + consensus's graceful descent.** So if
  panel starvation reappears, that is a signal to check ROTATION ELIGIBILITY
  (flags `use_model_rotation_*` / `use_per_model_cap_tracking`, provider
  `models[]` health, cooldown state) — NOT a reason to re-pin a heavy model into
  tier-2. Re-pinning is the wrong lever and reintroduces the daily-cap burn.
- **Discovery is now ADD-and-remove** (text-capability filtered: tag-first,
  probe-fallback), not prune-only — so a vendor deprecation self-heals (dead ids
  pruned, survivors adopted) without manual seed edits going forward.
