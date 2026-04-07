# stock-valuation
value investing sanity checker

# Company Growth Analyzer — Comprehensive Valuation & Growth Dashboard

**Version:** 4.0
**Language:** Python 3.10+
**Data Source:** Yahoo Finance (via `yfinance`)

---

## Table of Contents

1. [Purpose](#purpose)
2. [What This Script Does](#what-this-script-does)
3. [Methodology](#methodology)
   - [Growth Evaluation](#growth-evaluation)
   - [Valuation Assessment](#valuation-assessment)
   - [Quality Assessment](#quality-assessment)
   - [Momentum Assessment](#momentum-assessment)
   - [Composite Scoring](#composite-scoring)
   - [Classification Flags](#classification-flags)
4. [Mechanism & Architecture](#mechanism--architecture)
   - [Script Flow](#script-flow)
   - [Data Pipeline](#data-pipeline)
   - [Resilience Layer](#resilience-layer)
   - [NI Recovery Strategies](#ni-recovery-strategies)
5. [Data Usage](#data-usage)
   - [Data Sources](#data-sources)
   - [Key Metrics Computed](#key-metrics-computed)
   - [Outputs Generated](#outputs-generated)
6. [Configuration](#configuration)
7. [Installation & Usage](#installation--usage)
8. [Limitations](#limitations)
   - [Data Quality](#data-quality)
   - [Methodological](#methodological)
   - [Environmental](#environmental)
9. [Evaluation & Interpretation Guide](#evaluation--interpretation-guide)
   - [How to Read the Composite Scores](#how-to-read-the-composite-scores)
   - [Interpreting Outperformers vs Overvalued](#interpreting-outperformers-vs-overvalued)
   - [Known Scoring Edge Cases](#known-scoring-edge-cases)
   - [Validation Checks Performed](#validation-checks-performed)
10. [Output Reference](#output-reference)

---

## Purpose

This script is a **quantitative screening tool** that identifies S&P 500 stocks
where earnings growth materially diverges from market capitalisation growth. The
core thesis is simple:

> **If a company's net income is growing significantly faster than its market cap,
> the market may not yet be pricing that growth in. Conversely, if market cap is
> growing much faster than earnings, the stock may be trading on sentiment rather
> than fundamentals.**

The script does **not** provide buy/sell recommendations. It is a **screening and
ranking framework** designed to surface candidates for further fundamental research.

### Who This Is For

- Equity analysts performing initial screening across large universes
- Portfolio managers looking for systematic growth/value signals
- Quantitative researchers prototyping multi-factor models
- Anyone who wants a structured, repeatable process for comparing growth metrics
  across hundreds of stocks

---

## What This Script Does

The script executes in three sequential parts:

| Part | What | Output |
|------|------|--------|
| **Part 1** | Analyses a user-configured watchlist (e.g., AAPL, MSFT, NVDA) | Summary tables, growth charts, P/E history |
| **Part 2** | Scans all ~503 S&P 500 constituents, identifies outperformers and overvalued stocks | Sorted tables, scatter plots, per-stock charts |
| **Part 3** | Builds a comprehensive valuation dashboard with composite scores | Ranked Excel/CSV, sector boxplots, quadrant charts |

For each stock that passes data quality checks, the script computes **~30 metrics**
spanning growth, valuation, quality, and momentum — then combines them into a single
composite score for ranking.

---

## Methodology

### Growth Evaluation

Growth is assessed through a **4-metric blend** combining historical actuals with
forward consensus estimates:

Growth Score = NI CAGR rank (30%) + Revenue CAGR rank (25%) + Forward EPS Growth rank (25%) + Forward Revenue Growth rank (20%)


#### Net Income CAGR (30% weight)

The primary growth signal. Computed as:

NI_CAGR = (NI_latest / NI_earliest) ^ (1 / years) - 1


- Uses annual income statement data from Yahoo Finance
- Lookback cascade: tries 5 years → 3 years → 1 year → maximum available
- Requires both earliest and latest NI to be positive (otherwise = NaN)
- For **turnaround stocks** (earliest NI < 0, latest > 0), the script substitutes
  revenue CAGR, then FCF CAGR, as a proxy since NI CAGR is mathematically undefined
  when crossing from negative to positive

#### Revenue CAGR (25% weight)

Same CAGR formula applied to Total Revenue. Revenue is less volatile than NI and
provides a complementary signal — a company can have erratic NI due to one-time
charges while still growing revenue steadily.

#### Forward EPS Growth (25% weight)

Analyst consensus estimate for next-year earnings growth, sourced from
`yfinance.info["earningsGrowth"]`. This is forward-looking and captures expected
inflection points that trailing metrics miss.

#### Forward Revenue Growth (20% weight)

Analyst consensus for next-year revenue growth, sourced from
`yfinance.info["revenueGrowth"]`. Lower weight because revenue estimates are
typically less differentiated than earnings estimates.

#### Ranking Method

Each metric is converted to a **percentile rank** (0–100) across the scanned
universe. A stock at the 90th percentile of NI CAGR scores 90 on that component.
Missing values default to 50 (neutral). This approach is robust to outliers and
doesn't require assumptions about distributions.

---

### Valuation Assessment

Valuation Score = Sector-relative trailing P/E rank (15%) + Sector-relative forward P/E rank (35%) + Universe PEG rank (25%) + FCF Yield rank (25%)


| Metric | Calculation | Notes |
|--------|-------------|-------|
| **Trailing P/E** | `Market Cap / Net Income` | Capped at 100x, floored at 2.0x (sub-2.0 = data error) |
| **Forward P/E** | From `yfinance.info["forwardPE"]` | Same cap/floor; highest weight (35%) since it's most actionable |
| **PEG Ratio** | `Forward P/E / Forward EPS Growth` | Capped at 5.0; fallback: `Trailing P/E / NI CAGR` |
| **FCF Yield** | `Free Cash Flow / Market Cap` | Higher = more attractive; no cap |

**Sector-relative scoring:** P/E ratios are ranked within each sector (where sector
has 5+ stocks), since a 30x P/E means very different things in Tech vs Utilities.
Stocks in small sectors fall back to universe-wide ranking.

**Lower P/E = higher score** (inverted ranking), meaning cheap stocks score well.

---

### Quality Assessment

Quality Score = ROIC/ROE rank (35%) + Cash Conversion rank (25%) + NI Stability rank (20%, inverted) + Net Margin rank (20%)


| Metric | Calculation | Notes |
|--------|-------------|-------|
| **ROIC** | `NOPAT / Invested Capital` | Uses effective tax rate from financials; for financial-sector stocks, ROE is used instead |
| **ROE** | `Net Income / Stockholders' Equity` | Fallback from `yfinance.info["returnOnEquity"]` if balance sheet calc fails |
| **Cash Conversion** | `FCF / Net Income × 100%` | Log-compressed soft cap (linear to 150%, then logarithmic) to prevent extreme values from dominating |
| **NI Stability (CoV)** | `StdDev(Annual NI) / Mean(Annual NI)` | **Inverted**: lower volatility = higher quality score. Requires 3+ years of data |
| **Net Margin** | `Net Income / Revenue × 100%` | Higher margin = higher quality |

---

### Momentum Assessment

Momentum Score = Percentile rank of (NI CAGR − MCap CAGR)


This is not price momentum — it measures whether **earnings growth is outpacing or
lagging market cap growth**. A positive gap suggests the market hasn't fully priced
in the fundamental improvement; a negative gap suggests the stock's valuation has
run ahead of its earnings.

---

### Composite Scoring

Composite Score = Valuation (30%) + Quality (25%) + Growth (25%) + Momentum (20%)


All four pillar scores (0–100) are combined using configurable weights. The default
weights slightly favour valuation because the tool's primary use case is identifying
mispriced growth.

**Minimum sample size:** Composite scoring requires 5+ stocks. With fewer, percentile
ranks become meaningless, so the script skips scoring and displays a warning.

---

### Classification Flags

Each stock is tagged with zero or more flags that provide context for interpreting
its scores:

| Flag | Trigger | Implication |
|------|---------|-------------|
| `TURNAROUND` | Earliest NI < 0, Latest NI > 0 | NI CAGR is undefined; uses revenue/FCF proxy |
| `CYCLICAL` | NI CoV > 0.8 in cyclical sector, or > 1.2 in any sector | CAGR may be misleading — earnings are volatile |
| `ONE_TIME` | \|NI CAGR\| > 100% | Likely driven by one-time items, not organic growth |
| `QUALITY_PREMIUM` | ROIC > 25% AND NI CoV < 0.3 | High-quality compounder — may deserve a premium valuation |
| `FINANCIAL` | Sector is Financials / Financial Services | ROIC is replaced by ROE; balance sheet metrics differ |

---

## Mechanism & Architecture

### Script Flow

┌─────────────────────────────────────────────────┐ 
│ SSL Fix + Import + Config │ 
└─────────────────────┬───────────────────────────┘ 
│ 
┌─────────────────────▼───────────────────────────┐ 
│ PART 1: Configured Tickers 
│ 
┌─────────────────────────────────────────┐ 
│ 
│ For each ticker: 
│
│ 1. yf_ticker_with_retry() 
│ 
│ 2. fetch_growth_data() 
│ 
│ 
├─ Income Statement │ 
│ │ 
│ ├─ Revenue (before NI recovery) │ │ 
│ 
│ ├─ NI Recovery (3 strategies) │ │ 
│ │ ├─ Balance Sheet → ROIC/ROE │ │ │ 
│ ├─ Cash Flow → FCF/Conversion │ │ │ 
│ ├─ Price History → MCap CAGR │ │ │ 
│ ├─ P/E + PEG │ │ │ │ └─ classify_stock() │ │ 
│ │ 3. Cache result for Part 2 reuse │ 
│ │ └─────────────────────────────────────────┘ 
│ │ Print tables + Generate charts │ 
└─────────────────────┬───────────────────────────┘ 
│ 45s cooldown 
┌─────────────────────▼───────────────────────────┐ 
│ PART 2: S&P 500 Full Scan │ │ 
┌─────────────────────────────────────────┐ │ 
│ │ Load S&P 500 list (GitHub CSV) │ │ 
│ │ For each of ~503 tickers: │ │ 
│ │ - Check cache (skip if in Part 1) │ │ 
│ │ - fetch_growth_data() + 0.2s sleep │ │ 
│ │ - Classify: Outperformer / Overvalued │ │ 
│ │ (NI CAGR vs MCap CAGR ± 20pp) │ │
│ └─────────────────────────────────────────┘ │
│ Print Outperformer + Overvalued tables │ 
│ Generate per-stock + comparison charts │
└─────────────────────┬───────────────────────────┘ 
│ ┌─────────────────────▼───────────────────────────┐ 
│ PART 3: Valuation Dashboard │ │ 
┌─────────────────────────────────────────┐ │ 
│ │ compute_composite_scores() │ │
│ │ - Percentile rank each pillar │ │ 
│ │ - Weighted average → Composite Score │ │
│ │ - Sort + Rank │ 
│ │ └─────────────────────────────────────────┘ 
│ │ Charts: Sector P/E, P/E Decomp, FCF vs │
│ Growth, ROIC vs P/E, Composite Bar │ 
│ Exports: CSV, Excel (multi-sheet) │
└─────────────────────────────────────────────────┘


### Data Pipeline

For each ticker, `fetch_growth_data()` makes the following API calls:

stock = yf.Ticker(ticker) → .info, .income_stmt, .balance_sheet, .cashflow, .quarterly_income_stmt
yf_download_with_retry() → Monthly price history

Each call goes through the retry wrapper with exponential backoff (2s, 4s, 8s, 16s,
32s). Between tickers, a 0.2s sleep prevents burst-rate throttling.

### Resilience Layer

| Problem | Solution |
|---------|----------|
| Yahoo returns empty `.info` | `yf_ticker_with_retry()` — up to 5 retries with exponential backoff |
| Price download fails | `yf_download_with_retry()` — same retry logic, catches `TypeError` from yfinance internals |
| Income statement has NaN values | 3-strategy NI recovery (see below) |
| Corporate proxy blocks SSL | Global SSL verification disable + `requests.Session` monkey-patch |
| yfinance logs curl errors | `logging.getLogger("yfinance").setLevel(CRITICAL)` suppresses internal noise |
| Shares outstanding unavailable | 3-fallback chain: `sharesOutstanding` → `impliedSharesOutstanding` → `marketCap / price` |

### NI Recovery Strategies

When Yahoo returns income statement structure with NaN cell values (a common
degraded-response mode), the script attempts three recovery strategies in order:

Strategy 1: Quarterly Aggregation (no extra API call) ├─ Latest NI: Sum last 4 quarterly NI values (TTM) │ Annualise from 3 quarters (× 4/3) or 2 quarters (× 2) └─ Earliest NI: Sum earliest 4 quarterly NI values Annualise from 2–3 quarters

Strategy 2: Fresh Re-fetch (1 extra API call, 1.5s delay) └─ Create new yf.Ticker() object → read .income_stmt again (sometimes a fresh request gets non-degraded data)

Strategy 3: Revenue-Ratio Proxy (no extra API call) └─ If latest NI is known but earliest is NaN: NI_earliest ≈ NI_latest × (Revenue_earliest / Revenue_latest) Assumes margin was roughly constant — imperfect but directional


---

## Data Usage

### Data Sources

| Source | What | How |
|--------|------|-----|
| **Yahoo Finance API** (via `yfinance`) | Financial statements, price history, forward estimates, company info | `yf.Ticker()`, `yf.download()` |
| **GitHub CSV** (primary) | S&P 500 constituent list + GICS sector mapping | `datasets/s-and-p-500-companies` repo |
| **Wikipedia** (fallback) | S&P 500 constituent list | HTML table scraping |

### Key Metrics Computed

| Category | Metrics |
|----------|---------|
| **Growth** | NI CAGR, Revenue CAGR, MCap CAGR, FCF CAGR, Forward EPS Growth, Forward Revenue Growth |
| **Valuation** | Trailing P/E, Forward P/E, PEG Ratio, FCF Yield, P/E Expansion CAGR |
| **Quality** | ROIC, ROE, Net Margin, Cash Conversion, NI Stability (CoV), Invested Capital |
| **Classification** | TURNAROUND, CYCLICAL, ONE_TIME, QUALITY_PREMIUM, FINANCIAL flags |
| **Composite** | Valuation Score, Quality Score, Growth Score, Momentum Score, Composite Score, Rank |

### Outputs Generated

| Type | File | Description |
|------|------|-------------|
| **CSV** | `sp500_composite_ranking.csv` | Full ranked list with scores |
| **CSV** | `sp500_outperformers.csv` | Outperformer summary |
| **CSV** | `sp500_overvalued.csv` | Overvalued summary |
| **Excel** | `sp500_dashboard.xlsx` | Multi-sheet workbook (Composite, Outperformers, Overvalued) |
| **PNG** | `{TICKER}_growth.png` | Annual MCap + NI dual-axis chart |
| **PNG** | `{TICKER}_pe_history.png` | Historical P/E with mean + forward lines |
| **PNG** | `{TICKER}_quarterly.png` | Quarterly MCap + NI dual-axis chart |
| **PNG** | `sp500_growth_scatter.png` | NI CAGR vs MCap CAGR with ±20pp bands |
| **PNG** | `sp500_outperformers_bar.png` | Side-by-side MCap vs NI CAGR bars |
| **PNG** | `sp500_overvalued_bar.png` | Same for overvalued stocks |
| **PNG** | `sp500_sector_pe.png` | P/E boxplot by sector |
| **PNG** | `sp500_pe_decomp.png` | MCap growth decomposed into earnings growth + P/E expansion |
| **PNG** | `sp500_fcf_vs_growth.png` | FCF Yield vs NI CAGR scatter (top tickers labelled) |
| **PNG** | `sp500_roic_pe.png` | ROIC vs P/E quadrant chart (top tickers labelled) |
| **PNG** | `sp500_composite_bar.png` | Stacked composite score bar chart |

---

## Configuration

All parameters are in the `CONFIG` dictionary at the top of the script:

```python
CONFIG = {
    "tickers": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "NVO"],  # Watchlist
    "lookback_years": 5,                    # Growth calculation window
    "outperformance_threshold_pp": 20,      # Percentage-point gap for classification
    "run_sp500_scan": True,                 # Set False to skip Part 2+3
    "plot_quarterly": True,                 # Generate quarterly charts
    "chart_style": "seaborn-v0_8-whitegrid",
    "output_dir": "analysis/growth_analysis_output",
    "show_plots": False,                    # True = display in window (blocks)
    "run_valuation_dashboard": True,        # Set False to skip Part 3
    "composite_weights": {                  # Must sum to 1.0
        "valuation": 0.30,
        "quality":   0.25,
        "growth":    0.25,
        "momentum":  0.20,
    },
    "dashboard_top_n": 25,                  # How many stocks in top/bottom tables
    "pe_cap": 100,                          # P/E capped at this (outlier control)
    "pe_floor": 2.0,                        # P/E below this = data error → NaN
    "peg_cap": 5.0,                         # PEG capped at this
    "inter_part_cooldown": 45,              # Seconds between Part 1 and Part 2
    "per_ticker_sleep": 0.2,                # Seconds between each ticker fetch
}
```

Installation & Usage
Requirements
pip install yfinance pandas numpy matplotlib seaborn tqdm beautifulsoup4 requests openpyxl
Running
python company_growth_analyzer.py
Typical runtime: ~20–25 minutes for a full S&P 500 scan (depends on network conditions and Yahoo rate limiting).

Output Location
All files are saved to
analysis/growth_analysis_output/
by default (configurable via
CONFIG["output_dir"]
).

Limitations
Data Quality
Limitation	Impact	Mitigation
Yahoo Finance data is unofficial and can be degraded	60–80% of S&P 500 tickers may return NaN net income values in a given run	3-strategy NI recovery (TTM, re-fetch, revenue proxy)
Corporate proxy/firewall may block Yahoo	SSL errors, timeouts, reduced scan yield	Global SSL disable, retry with backoff, error suppression
Scan yield varies by run	Results are non-deterministic; different days produce different success rates	Rejection summary tracks exact failure modes
ADR / foreign stocks may have incorrect data	P/E, shares outstanding, sector mapping can be wrong for non-US domiciled companies	P/E floor (2.0) catches some errors; manual verification recommended
Forward estimates are consensus, not fact	
earningsGrowth
and
revenueGrowth
reflect analyst average, which can be stale or biased	Lower weight (20–25%) in Growth score; used alongside trailing actuals
Quarterly data may be incomplete	TTM calculation requires 4 quarters; annualisation from 2–3 is approximate	Annualisation factor explicitly applied; flagged in debug logs
Methodological
Limitation	Impact	Context
CAGR requires positive start and end values	Stocks going from positive to negative NI (or vice versa) get NaN CAGR	Turnaround stocks use revenue/FCF proxy; negative-to-negative is excluded
Percentile ranking with <100 stocks	With only 80–100 valid stocks (out of 503), rankings have wide confidence intervals	A stock at the 90th percentile of 80 might be 70th of 500
Survivorship bias	Stocks that return clean data tend to be large, liquid, frequently-queried	Missing stocks are disproportionately smaller/newer S&P members
No sector-relative Growth or Momentum scores	Growth and Momentum are ranked universe-wide, not within sector	Only Valuation P/E uses sector-relative ranking
Static lookback window	5-year CAGR may span economic cycles unevenly	Lookback cascade (5Y → 3Y → 1Y) partially addresses this
Cash conversion soft cap	Log compression above 150% still allows some clustering	Better than hard cap at ±200% but not perfect
No risk adjustment	Composite score doesn't penalise for beta, leverage, or drawdown	Quality pillar (CoV, ROIC) partially captures this
Environmental
Limitation	Detail
Rate limiting	Yahoo's free API throttles at ~2,000 requests/hour; the 0.2s sleep helps but doesn't guarantee all stocks succeed
No caching between runs	Each run fetches everything from scratch; there is no local database or disk cache
Single-threaded	Sequential processing; parallelism would be faster but increases rate-limit risk
Memory	All records held in memory; not an issue for 500 stocks but would be for 10,000+
Evaluation & Interpretation Guide
How to Read the Composite Scores
Score Range	Interpretation
75–100	Strong across multiple dimensions — likely a well-valued, high-quality growth stock
60–74	Above average; may excel in 2–3 pillars but have weakness in others
40–59	Average; often indicates mixed signals (e.g., cheap but low quality)
25–39	Below average; typically overvalued, low quality, or declining growth
0–24	Weakest in the universe; usually cyclical downturns or data issues
Important: Composite scores are relative to the scanned universe, not absolute. A score of 80 means "better than 80% of the S&P 500 stocks that returned valid data in this run." Different runs may produce different scores for the same stock.

Interpreting Outperformers vs Overvalued
Outperformers (NI CAGR > MCap CAGR + 20pp):

Earnings are growing much faster than the stock price suggests
The market may be undervaluing the company's growth trajectory
Check: Is the growth sustainable? Or driven by one-time items?
Check: Are forward estimates consistent with trailing growth?
Overvalued (MCap CAGR > NI CAGR + 20pp):

Stock price has risen much faster than earnings justify
Could reflect sentiment, sector rotation, or growth expectations not yet in earnings
Check: Are forward estimates strong? (High fwd growth may justify the premium)
Check: Is the NI CAGR depressed by a cyclical trough or one-time charge?
Known Scoring Edge Cases
Scenario	What Happens	How to Interpret
<5 stocks pass validation	Composite scoring is skipped entirely	Not enough data for meaningful ranking
TURNAROUND stock	NI CAGR replaced by Revenue CAGR or FCF CAGR in Growth score	Growth score is approximate — verify with quarterly trends
FINANCIAL stock	ROIC replaced by ROE in Quality score	Banking/insurance capital structures make ROIC unreliable
ONE_TIME flag	NI CAGR > 100%	Likely driven by asset sales, write-downs, or restructuring — not organic
CYCLICAL flag	NI CoV > 0.8 in cyclical sector	CAGR start/end points may be misleading depending on cycle position
P/E < 2.0	Treated as data error → NaN	Prevents clearly impossible values (like 1.2x for a $400B company) from distorting scores
Cash Conversion > 150%	Log-compressed (not hard-capped)	Values above 150% are compressed but still differentiated
Validation Checks Performed
The script includes several built-in data quality safeguards:

Check	Threshold	Action
.info
dict empty	—	Reject ticker (up to 5 retries first)
Income statement empty	—	Reject ticker
Fewer than 2 statement periods	—	Reject ticker
Year span < 0.5 years	0.5y minimum	Reject ticker
NI row not found in statement	—	Reject ticker
NI values are NaN	—	3-strategy recovery → reject if all fail
Shares outstanding unavailable	—	3-fallback chain → reject if all fail
Price history empty / NaN	—	Reject ticker (up to 5 retries first)
MCap earliest ≤ 0	—	Reject ticker
MCap time span < 0.5 years	0.5y minimum	Reject ticker
P/E < 2.0	2.0 floor	Treated as NaN (data error)
P/E > 100	100 cap	Capped to prevent outlier distortion
PEG > 5.0	5.0 cap	Capped
Cash Conversion > 150%	Soft cap	Log-compressed
All rejections are tracked and summarised at the end of each scan part with exact counts and percentages per reason.

Output Reference
Console Output Structure
PART 1 — Configured tickers
  Per-ticker OK/SKIP log
  Summary Table (Ticker, MCap CAGR, NI CAGR, delta, flags)
  Valuation Table (P/E, PEG, FCF Yield, ROIC, forward estimates)
  Rejection Summary

PART 2 — S&P 500 Full Scan
  Progress bar (503 tickers)
  Scan Statistics (valid count, outperformer/overvalued count)
  Rejection Summary
  2A: Outperformer Tables + Charts
  2B: Overvalued Tables + Charts

PART 3 — Valuation Dashboard
  Top 25 Composite Ranked (with all pillar scores)
  Bottom 25 Composite Ranked
  File listing with sizes
Chart Descriptions
Chart	What It Shows	How to Read It
{TICKER}_growth.png
Dual-axis: MCap ($B, blue line) vs NI ($B, green/red bars) over time	Diverging lines suggest mispricing; green bars = profit, red = loss
{TICKER}_pe_history.png
P/E ratio over time with mean line and forward P/E line	Current P/E vs historical average reveals expansion/compression
{TICKER}_quarterly.png
Same as growth but quarterly frequency	More granular trend visibility
sp500_growth_scatter.png
Every stock plotted by MCap CAGR (x) vs NI CAGR (y)	Green dots above +20pp line = outperformers; red below −20pp = overvalued
sp500_sector_pe.png
Box-and-whisker P/E by sector	Compare a stock's P/E to its sector's distribution
sp500_pe_decomp.png
Stacked bars: Earnings CAGR + P/E Expansion = MCap CAGR	Reveals whether returns came from growth or multiple expansion
sp500_fcf_vs_growth.png
FCF Yield (y) vs NI CAGR (x), coloured by sector	Top-right quadrant = growing AND cash-generative (ideal)
sp500_roic_pe.png
ROIC (y) vs P/E (x), colour-coded quadrants	Green quadrant (high ROIC, low P/E) = highest quality at fair price
sp500_composite_bar.png
Stacked bar: 4 pillar scores for top 25 stocks	Height = total composite; colour breakdown shows which pillars drive the score
Disclaimer
This tool is for informational and educational purposes only. It does not constitute investment advice, and the outputs should not be used as the sole basis for any investment decision. All data is sourced from Yahoo Finance and may contain errors, omissions, or delays. Past performance does not guarantee future results. Always perform independent due diligence before making investment decisions.


This README covers everything a user or reviewer needs to understand, run, and critically evaluate the script. Let me know if you'd like me to adjust any section or add anything else.
