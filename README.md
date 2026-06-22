# Tired Market

> 🚧 **Work in progress.** Actively-developed personal project, shared as-is. Things may break, change, or be incomplete. No support or stability guaranteed.
>
> ⚠️ **Use at your own risk.** Experimental software, provided as-is with **no warranty**. For **educational / research purposes only** — **NOT financial advice**. The author is not responsible for any damages, data loss, financial losses, or decisions made using this software.

Licensed under **AGPL-3.0** — see [LICENSE](LICENSE). You may use and modify it freely; if you distribute or host a modified version, you must release your source under the same license.

---

AI-assisted, multi-model stock-pick research and paper-trading workflow.
Cross-references several language models, surfaces structured BUY / WATCH /
AVOID verdicts, and groups them into deployment baskets (ALL IN / A FEW /
DIVERSIFY) for a budget you set. Educational / research tool — not
financial advice.

## Download

Most people: grab a ready-to-run build from the [Releases page](https://github.com/mseals/Tiredmarket/releases/latest).

- **All-in-One** (`TiredMarket-AllInOne-v4.14.6.108.exe`) — one file. Download, double-click, it runs. No install, no unzip. Simplest option.
- **Installer** (`TiredMarket-Setup-v4.14.6.108.exe`) — installs like a normal program (Start Menu shortcut, clean uninstall that can remove your data). Recommended if you want it installed properly.
- **Portable** (`TiredMarket-portable-v4.14.6.108.zip`) — unzip the folder anywhere and run `TiredMarket.exe` inside. No install. Good for a USB stick.
- **Source** (for developers) — see "Running from source" below.

> ⚠️ **Unsigned software.** These builds are unsigned, so Windows SmartScreen may say "Windows protected your PC" → click **More info → Run anyway**. Some antivirus may flag it (a false positive — normal for independent free apps built with PyInstaller). The All-in-One single file trips this a bit more than the others.

### System requirements
- **Windows 10 or 11, 64-bit.** (Not macOS, Linux, older Windows, or 32-bit.)
- **No GPU needed** — the AI runs in the cloud (bring your own free keys), and the app itself is CPU-only.
- **Minimum:** ~4 GB RAM, dual-core, ~3 GB free disk.
- **Recommended:** 8 GB RAM + SSD.
- The first hour of data-loading is the heavy part (price + news cache fill from scratch); it's light after that. Data grows over time toward ~2 GB.
- **AI is optional:** bring your own free API keys for smarter, validated picks — or run on the built-in algorithm with no keys at all.

## Where your data lives

Your market cache, predictions, settings, **saved API keys, and
portfolio** are stored in a `data` folder:

- **Installer:** beside the install (e.g. `C:\TiredMarket\data`).
- **Portable:** the location you pick on first run.
- A tiny pointer file in your Windows user profile records that location;
  the app finds it on every launch. See **Settings → Data Location** in
  the app.
- **Uninstalling** (installer) offers to remove the data folder — which
  includes your saved API keys and portfolio. Choose "No" to keep it for
  a reinstall.
- **Multi-user note:** the pointer is per-Windows-user, so a *different*
  OS user on the same PC is asked for a data location on their first run.

## Running from source

- Windows + Python 3.10 or newer (`python --version`).
- One or more free API keys. The simplest start is Groq's free tier
  ([https://console.groq.com](https://console.groq.com)) — sign up,
  create a key, paste it into the app. Other supported providers (any
  combination): Google AI Studio, Mistral La Plateforme, Cerebras,
  Zhipu (Z.AI), GitHub Models, Cloudflare Workers AI.
- Internet (for model calls + Yahoo Finance / Stooq price data).

### Install (from source)

1. Clone or download the repo anywhere (folder name and drive don't
   matter — the app resolves its own paths at runtime).
2. Double-click `INSTALL.bat` to install Python dependencies (or run
   `pip install -r requirements.txt` from a terminal in the folder).
3. Double-click `START.bat` to launch (or run `python tired_market.py`).

On first launch the app copies `data/api_providers.example.json` and
`data/config.example.json` into live (gitignored) `data/api_providers.json`
and `data/config.json`, then creates its own empty databases. Your local
keys and state live in those gitignored files — they will never be
committed if you push your own fork.

## First run

1. Accept the one-time disclaimer (educational use, not financial advice).
2. Open **Settings → API Providers** and paste your API key for at
   least one provider. Toggle **Enabled** on for the providers you
   want to use. Save.
3. The app's local price/news cache fills over the first hour or so as
   it pulls daily bars and news for the universe. You can leave it
   running in the background.
4. After the first scan cycle completes, the **Recommend** window will
   populate with picks per Trading style (Under $5, $5–$10, $10–$50,
   $50+). It is normal to see "Filling" while the initial cache builds.

## Using the Recommend board

- **Trading style** picks the price band; each pick is from that band's
  scan.
- **Cash to deploy** sizes the displayed positions across three columns:
  - **ALL IN** — one position, max conviction.
  - **A FEW** — 2–3 positions; one big hit makes the basket green.
  - **DIVERSIFY** — 4–6 positions; smaller per-position swings.
- Each pick shows a **consensus badge**: `✓ Validated`, `Mixed (B/W/A)`,
  `n/m WAIT`, etc. The badge is the multi-model second-opinion summary.
- Clicking **Verify** on a pick runs a fresh multi-model consensus vote
  on demand.
- Consensus that comes back as a clear AVOID majority disqualifies the
  pick from the board (it's not a buy at all). Ambiguous "undecided"
  consensus is surfaced with the Mixed badge so you see the
  disagreement.

## Data sources

This tool fetches market data from third-party sources at runtime:

- **Yahoo Finance** via [`yfinance`](https://github.com/ranaroussi/yfinance)
  (Apache-2.0). yfinance is an unofficial community library that calls
  Yahoo's internal endpoints; it is not affiliated with or endorsed by
  Yahoo. Yahoo's terms treat this data as personal-use — please review
  Yahoo's terms and use this tool for personal / educational research only.
- **Stooq** (free end-of-day data) as a fallback when Yahoo is
  rate-limited.
- **SEC EDGAR** (public-domain fundamentals). EDGAR asks for a real
  contact email in the request User-Agent — set yours in Settings →
  SEC contact.
- **Finnhub** (free tier) for the earnings calendar — bring your own
  free key.

You bring your own API keys and your own data usage. **You are
responsible for complying with each source's terms.** This project does
**NOT** redistribute any third-party data — every user fetches their own.

## LLM providers (bring your own keys)

The tool calls language-model APIs via providers you choose. All are
free-tier accessible at the time of writing — you supply your own key
for each:

- **Groq** — recommended starter ([console.groq.com](https://console.groq.com))
- **Google AI Studio (Gemini)** — note: free-tier prompts may be used
  for model training unless you opt out
- **Mistral La Plateforme**, **Cerebras**, **Zhipu AI (Z.AI)**,
  **GitHub Models**, **Cloudflare Workers AI** (needs account ID + key),
  **OpenRouter**, **SambaNova**

Free tiers are rate-limited and not intended for high-volume production
use. Review each provider's privacy and acceptable-use policy before
sending data; don't paste anything sensitive or non-public.

## Disclaimer

This software is for educational and research purposes only. It is NOT
financial, investment, or trading advice. Outputs from language models
can be wrong, stale, or hallucinated. Do not invest money based on this
tool's recommendations without independent research. You are responsible
for your own trading decisions and any losses.

## Troubleshooting

- **Logs:** `data/activity.log` records every action the app takes. Open
  it in any text editor when something looks off.
- **Reset the database:** close the app, delete `data/tired_market.db`
  (and optionally `data/cache.db`), relaunch. The app re-creates both
  empty and re-fills the cache.
- **Resetting providers:** edit `data/api_providers.json` directly, or
  use the Providers dialog in the UI.
- **Performance:** the first hour of fill is heavy (price cache + news
  cache populate from scratch). Subsequent launches are fast because
  the cache is incremental.

## Notices

"Yahoo" and "Yahoo Finance" are trademarks of Yahoo. This project is not
affiliated with or endorsed by Yahoo. Thanks to the
[`yfinance`](https://github.com/ranaroussi/yfinance) project for the
data adapter, and to Groq, Google, Mistral, Cerebras, Zhipu, GitHub,
Cloudflare, OpenRouter, and SambaNova for free-tier API access.
