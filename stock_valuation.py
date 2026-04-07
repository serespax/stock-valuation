#!/usr/bin/env python3
"""
Company Growth Analyzer — Comprehensive Valuation & Growth Dashboard v4
========================================================================
v4 fixes (data pipeline — targeted):
  - REMOVED batch price download (triggers SSL storms on corporate proxies)
  - Reduced per-ticker sleep to 0.2s (v3's 0.5s doubled runtime for no gain)
  - Suppressed yfinance internal curl/SSL error spam
  - 3-strategy NI recovery: TTM quarterly -> fresh re-fetch -> revenue proxy
  - Revenue computed BEFORE NI recovery (enables revenue-ratio proxy)
  - Annualization from 2-3 quarterly datapoints when 4 aren't available
  - Increased inter-part cooldown to 45s

Unchanged from v3 (working correctly):
  - Sector-relative P/E scoring, forward-weighted composite
  - P/E floor (2.0) and cap (100), PEG cap (5.0), soft cash-conv cap
  - ROIC + ROE fallback for financials
  - Turnaround-aware growth proxy (rev CAGR / FCF CAGR)
  - Classification flags: TURNAROUND, CYCLICAL, ONE_TIME, QUALITY_PREMIUM, FINANCIAL
  - Forward revenue growth in Growth sub-score (4-metric blend)
  - Small-N composite guard (< 5 stocks = skip)
  - Rejection reason tracking + summary

Requirements:
    pip install yfinance pandas numpy matplotlib seaborn tqdm beautifulsoup4 requests openpyxl

Usage:
    1. Edit the CONFIG section below.
    2. Run: python company_growth_analyzer.py
"""

# =============================================================================
# SSL FIX — Must be FIRST
# =============================================================================
import ssl
import os

try:
    import pip_system_certs  # noqa: F401
except ImportError:
    pass
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

if not os.environ.get("SSL_CERT_FILE"):
    os.environ["PYTHONHTTPSVERIFY"] = "0"
    ssl._create_default_https_context = ssl._create_unverified_context

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# =============================================================================

import warnings
warnings.filterwarnings("ignore")

import yfinance as yf
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.dates as mdates
import seaborn as sns
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm
from datetime import datetime
from io import StringIO
from collections import Counter
import logging
import time
import re

# ------------------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------------------
CONFIG = {
    "tickers": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "NVO"],
    "lookback_years": 5,
    "outperformance_threshold_pp": 20,
    "run_sp500_scan": True,
    "plot_quarterly": True,
    "chart_style": "seaborn-v0_8-whitegrid",
    "output_dir": os.path.join('valuation_output'), 
    "show_plots": False,
    "run_valuation_dashboard": True,
    "composite_weights": {
        "valuation": 0.30,
        "quality":   0.25,
        "growth":    0.25,
        "momentum":  0.20,
    },
    "dashboard_top_n": 25,
    "pe_cap": 100,
    "pe_floor": 2.0,
    "peg_cap": 5.0,
    "inter_part_cooldown": 45,
    "per_ticker_sleep": 0.2,
}

MAX_RETRIES = 5
BASE_SLEEP = 2
RETRYABLE_ERRORS = [
    "429", "rate", "Too Many", "RemoteDisconnected",
    "timeout", "empty", "ConnectionError", "ConnectionReset",
    "ChunkedEncodingError", "ReadTimeout",
]

# ------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Suppress yfinance internal curl/SSL error spam
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("peewee").setLevel(logging.CRITICAL)

_REJECTION_REASONS: Counter = Counter()
_SP500_SECTOR_MAP: dict[str, str] = {}
_FETCHED_RECORDS_CACHE: dict[str, dict] = {}


# ==============================================================================
# RESILIENT YFINANCE WRAPPERS
# ==============================================================================

def yf_download_with_retry(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
    period: str | None = None,
    interval: str = "1d",
    max_retries: int = MAX_RETRIES,
    base_sleep: int = BASE_SLEEP,
    silent: bool = False,
) -> pd.DataFrame | None:
    for attempt in range(1, max_retries + 1):
        try:
            kw = {"progress": False, "interval": interval}
            if period:
                kw["period"] = period
            else:
                if start:
                    kw["start"] = start
                if end:
                    kw["end"] = end
            raw = yf.download(ticker, **kw)
            if raw is None or raw.empty:
                raise ValueError("empty DataFrame")
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            return raw
        except TypeError:
            if attempt < max_retries:
                time.sleep(base_sleep ** attempt)
            else:
                return None
        except Exception as e:
            err = str(e)
            if attempt < max_retries and any(
                k.lower() in err.lower() for k in RETRYABLE_ERRORS
            ):
                time.sleep(base_sleep ** attempt)
            else:
                return None
    return None


def yf_ticker_with_retry(
    ticker: str,
    max_retries: int = MAX_RETRIES,
    base_sleep: int = BASE_SLEEP,
) -> yf.Ticker | None:
    """Relaxed gate: accepts any non-empty .info dict."""
    for attempt in range(1, max_retries + 1):
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            if not info:
                if attempt < max_retries:
                    raise ValueError("empty info")
                return None
            return stock
        except TypeError:
            if attempt < max_retries:
                time.sleep(base_sleep ** attempt)
            else:
                return None
        except Exception as e:
            err = str(e)
            if attempt < max_retries and any(
                k.lower() in err.lower() for k in RETRYABLE_ERRORS
            ):
                time.sleep(base_sleep ** attempt)
            else:
                return None
    return None


# Patch SSL for yfinance internals
_orig_get = requests.Session.get
_orig_post = requests.Session.post


def _patched_get(self, *args, **kwargs):
    kwargs.setdefault("verify", False)
    return _orig_get(self, *args, **kwargs)


def _patched_post(self, *args, **kwargs):
    kwargs.setdefault("verify", False)
    return _orig_post(self, *args, **kwargs)


requests.Session.get = _patched_get
requests.Session.post = _patched_post
log.info("SSL verification disabled globally (corporate proxy mode).")


# ==============================================================================
# S&P 500 TICKERS + SECTOR MAP
# ==============================================================================

def _get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "CompanyGrowthAnalyzer/4.0 python-requests",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    s.verify = False
    return s


def _is_valid_ticker(t: str) -> bool:
    if not t or len(t) > 10:
        return False
    t = t.strip()
    if t.lower() in ("nan", "none", "", "symbol", "ticker"):
        return False
    return bool(re.match(r"^[A-Z]{1,5}(-[A-Z]{1,2})?$", t.upper()))


def _clean_tickers(raw: list[str]) -> list[str]:
    return [
        t.upper()
        for t in (str(x).strip().replace(".", "-") for x in raw)
        if _is_valid_ticker(t)
    ]


def get_sp500_tickers() -> list[str]:
    global _SP500_SECTOR_MAP
    for name, func in [("GitHub CSV", _fetch_github), ("Wikipedia", _fetch_wiki)]:
        try:
            tickers = func()
            if tickers and len(tickers) > 400:
                log.info(f"S&P 500 loaded via {name}: {len(tickers)} tickers.")
                return tickers
        except Exception as e:
            log.warning(f"  {name} failed: {e}")
    return []


def _fetch_github() -> list[str]:
    global _SP500_SECTOR_MAP
    url = (
        "https://raw.githubusercontent.com/datasets/"
        "s-and-p-500-companies/main/data/constituents.csv"
    )
    resp = _get_session().get(url, timeout=20)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text))
    tcol = next(
        (c for c in df.columns if str(c).strip().lower() in ("symbol", "ticker")),
        df.columns[0],
    )
    scol = next(
        (c for c in df.columns if str(c).strip().lower() in ("sector", "gics sector")),
        None,
    )
    tickers = _clean_tickers(df[tcol].dropna().astype(str).tolist())
    if scol:
        for _, row in df.iterrows():
            sym = str(row[tcol]).strip().replace(".", "-").upper()
            sec = str(row[scol]).strip()
            if _is_valid_ticker(sym) and sec and sec.lower() != "nan":
                _SP500_SECTOR_MAP[sym] = sec
    log.info(f"  GitHub CSV: {len(tickers)} tickers, {len(_SP500_SECTOR_MAP)} sectors.")
    return tickers


def _fetch_wiki() -> list[str]:
    resp = _get_session().get(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", timeout=20
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", {"id": "constituents"})
    if not table:
        return []
    raw = [
        row.find_all("td")[0].text.strip()
        for row in table.find_all("tr")[1:]
        if row.find_all("td")
    ]
    return _clean_tickers(raw)


def get_sector(ticker: str, info: dict | None = None) -> str:
    if ticker in _SP500_SECTOR_MAP:
        return _SP500_SECTOR_MAP[ticker]
    if info and info.get("sector"):
        return info["sector"]
    return "Unknown"


# ==============================================================================
# HELPERS
# ==============================================================================

def ensure_output_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _find_ni_row(stmt: pd.DataFrame) -> str | None:
    for label in [
        "Net Income",
        "Net Income Common Stockholders",
        "NetIncome",
        "Net Income From Continuing Operations",
    ]:
        if label in stmt.index:
            return label
    return None


def _find_row(stmt: pd.DataFrame, candidates: list[str]) -> str | None:
    for label in candidates:
        if label in stmt.index:
            return label
    return None


def _sg(d: dict, key: str, default=np.nan):
    val = d.get(key, default)
    return default if val is None else val


def _safe_cagr(s: float, e: float, y: float) -> float:
    if pd.isna(s) or pd.isna(e) or y < 0.5:
        return np.nan
    if s > 0 and e > 0:
        return ((e / s) ** (1 / y) - 1) * 100
    return np.nan


def _cap(val: float, lo: float = None, hi: float = None) -> float:
    if pd.isna(val):
        return val
    if lo is not None and val < lo:
        return lo
    if hi is not None and val > hi:
        return hi
    return val


def _soft_cap_cc(raw_cc: float) -> float:
    """Log-compressed cash conversion: linear to 150%, log beyond."""
    if pd.isna(raw_cc):
        return raw_cc
    sign = 1 if raw_cc >= 0 else -1
    abs_cc = abs(raw_cc)
    if abs_cc <= 150:
        return raw_cc
    return sign * (150 + np.log1p(abs_cc - 150) * 25)


def _resolve_shares(info: dict) -> float:
    """3-strategy shares outstanding fallback."""
    for key in ["sharesOutstanding", "impliedSharesOutstanding"]:
        v = _sg(info, key)
        if not pd.isna(v) and v > 0:
            return v
    mcap = _sg(info, "marketCap")
    for pk in ["regularMarketPrice", "currentPrice", "previousClose"]:
        p = _sg(info, pk)
        if not pd.isna(p) and p > 0 and not pd.isna(mcap) and mcap > 0:
            return mcap / p
    return np.nan


def _compute_ttm_ni(stock: yf.Ticker) -> float:
    """TTM net income from quarterly statements. Annualizes from 2-3 quarters."""
    try:
        q = stock.quarterly_income_stmt
        if q is None or q.empty:
            return np.nan
        ni_row = _find_ni_row(q)
        if ni_row is None:
            return np.nan
        dates = sorted(q.columns, reverse=True)
        vals = []
        for d in dates[:4]:
            v = q.loc[ni_row, d]
            if not pd.isna(v):
                vals.append(v)
        if len(vals) >= 4:
            return sum(vals)
        if len(vals) >= 3:
            return sum(vals) * (4 / 3)
        if len(vals) >= 2:
            return sum(vals) * 2
    except Exception:
        pass
    return np.nan


def _compute_earliest_annual_ni_from_quarterly(stock: yf.Ticker) -> float:
    """Sum earliest 4 (or 2-3 annualized) quarterly NI for earliest annual."""
    try:
        q = stock.quarterly_income_stmt
        if q is None or q.empty:
            return np.nan
        ni_row = _find_ni_row(q)
        if ni_row is None:
            return np.nan
        dates = sorted(q.columns)  # oldest first
        vals = []
        for d in dates[:4]:
            v = q.loc[ni_row, d]
            if not pd.isna(v):
                vals.append(v)
        if len(vals) >= 4:
            return sum(vals)
        if len(vals) >= 3:
            return sum(vals) * (4 / 3)
        if len(vals) >= 2:
            return sum(vals) * 2
    except Exception:
        pass
    return np.nan


# ==============================================================================
# CLASSIFICATION
# ==============================================================================

def classify_stock(r: dict) -> list[str]:
    flags = []
    nie, nil = r.get("ni_earliest", np.nan), r.get("ni_latest", np.nan)
    nic = r.get("ni_cagr", np.nan)
    cov = r.get("ni_stability_cov", np.nan)
    roic = r.get("roic", np.nan)
    sec = r.get("sector", "")

    if not pd.isna(nie) and not pd.isna(nil) and nie < 0 and nil > 0:
        flags.append("TURNAROUND")
    cyc_secs = {
        "Energy", "Materials", "Industrials",
        "Consumer Discretionary", "Consumer Cyclical",
    }
    if not pd.isna(cov) and ((cov > 0.8 and sec in cyc_secs) or cov > 1.2):
        flags.append("CYCLICAL")
    if not pd.isna(nic) and abs(nic) > 100:
        flags.append("ONE_TIME")
    if not pd.isna(roic) and roic > 25 and not pd.isna(cov) and cov < 0.3:
        flags.append("QUALITY_PREMIUM")
    if sec in {"Financials", "Financial Services"}:
        flags.append("FINANCIAL")
    return flags


# ==============================================================================
# MASTER DATA FETCH
# ==============================================================================

def fetch_growth_data(ticker: str, lookback_years: int) -> dict | None:

    def _reject(reason: str) -> None:
        _REJECTION_REASONS[reason] += 1
        log.debug(f"  {ticker}: REJECTED — {reason}")

    try:
        stock = yf_ticker_with_retry(ticker)
        if stock is None:
            _reject("yf_ticker_retry_exhausted")
            return None

        info = stock.info or {}

        # =============================================================
        # INCOME STATEMENT
        # =============================================================
        income_stmt = stock.income_stmt
        if income_stmt is None or income_stmt.empty:
            _reject("income_stmt_empty")
            return None

        dates_sorted = sorted(income_stmt.columns)
        if len(dates_sorted) < 2:
            _reject("income_stmt_<2_periods")
            return None

        # Lookback cascade: configured -> 3Y -> 1Y -> max
        latest_date = dates_sorted[-1]
        earliest_date, year_span = None, 0.0
        for lb in [lookback_years, 3, 1]:
            cutoff = latest_date - pd.DateOffset(years=lb + 1)
            avail = [d for d in dates_sorted if d >= cutoff]
            if len(avail) >= 2:
                earliest_date = avail[0]
                latest_date = avail[-1]
                year_span = (latest_date - earliest_date).days / 365.25
                if year_span >= 0.5:
                    break
        if earliest_date is None or year_span < 0.5:
            earliest_date, latest_date = dates_sorted[0], dates_sorted[-1]
            year_span = (latest_date - earliest_date).days / 365.25
        if year_span < 0.5:
            _reject(f"year_span_too_short ({year_span:.2f}y)")
            return None

        ni_row = _find_ni_row(income_stmt)
        if ni_row is None:
            _reject("no_net_income_row")
            return None

        ni_earliest = income_stmt.loc[ni_row, earliest_date]
        ni_latest = income_stmt.loc[ni_row, latest_date]

        # =============================================================
        # REVENUE — computed BEFORE NI recovery (Strategy 3 needs it)
        # =============================================================
        rev_row = _find_row(
            income_stmt, ["Total Revenue", "Revenue", "Operating Revenue"]
        )
        rev_e, rev_l, rev_cagr = np.nan, np.nan, np.nan
        if rev_row:
            rev_e = income_stmt.loc[rev_row, earliest_date]
            rev_l = income_stmt.loc[rev_row, latest_date]
            rev_cagr = _safe_cagr(rev_e, rev_l, year_span)

        # =============================================================
        # DEGRADED-RESPONSE RECOVERY (3 strategies)
        # Yahoo often returns statement structure with NaN cell values.
        # =============================================================
        if pd.isna(ni_earliest) or pd.isna(ni_latest):

            # Strategy 1: TTM / earliest from quarterly statements
            if pd.isna(ni_latest):
                ttm = _compute_ttm_ni(stock)
                if not pd.isna(ttm):
                    ni_latest = ttm
                    log.debug(f"  {ticker}: S1 TTM NI latest = {ttm/1e9:.2f}B")

            if pd.isna(ni_earliest):
                eq = _compute_earliest_annual_ni_from_quarterly(stock)
                if not pd.isna(eq):
                    ni_earliest = eq
                    log.debug(f"  {ticker}: S1 quarterly NI earliest = {eq/1e9:.2f}B")

            # Strategy 2: Fresh Ticker object re-fetch
            if pd.isna(ni_earliest) or pd.isna(ni_latest):
                time.sleep(1.5)
                try:
                    fresh = yf.Ticker(ticker)
                    fresh_stmt = fresh.income_stmt
                    if fresh_stmt is not None and not fresh_stmt.empty:
                        ni_row2 = _find_ni_row(fresh_stmt)
                        if ni_row2:
                            fd = sorted(fresh_stmt.columns)
                            if len(fd) >= 2:
                                if pd.isna(ni_earliest):
                                    v = fresh_stmt.loc[ni_row2, fd[0]]
                                    if not pd.isna(v):
                                        ni_earliest = v
                                if pd.isna(ni_latest):
                                    v = fresh_stmt.loc[ni_row2, fd[-1]]
                                    if not pd.isna(v):
                                        ni_latest = v
                                # Update references if we recovered
                                if not pd.isna(ni_earliest) and not pd.isna(ni_latest):
                                    income_stmt = fresh_stmt
                                    dates_sorted = fd
                                    earliest_date, latest_date = fd[0], fd[-1]
                                    year_span = (
                                        latest_date - earliest_date
                                    ).days / 365.25
                except Exception:
                    pass

            # Strategy 3: Revenue-ratio proxy for earliest NI
            if (
                pd.isna(ni_earliest)
                and not pd.isna(ni_latest)
                and not pd.isna(rev_e)
                and not pd.isna(rev_l)
                and rev_l > 0
            ):
                ni_earliest = ni_latest * (rev_e / rev_l)
                log.debug(f"  {ticker}: S3 revenue-ratio NI earliest")

        if pd.isna(ni_earliest) or pd.isna(ni_latest):
            _reject("ni_value_NaN")
            return None

        # =============================================================
        # NI CAGR + GROWTH
        # =============================================================
        ni_cagr = _safe_cagr(ni_earliest, ni_latest, year_span)
        is_turnaround = bool(ni_earliest < 0 and ni_latest > 0)
        ni_total_growth = (
            ((ni_latest - ni_earliest) / abs(ni_earliest)) * 100
            if ni_earliest != 0
            else np.nan
        )

        # Margin
        net_margin = np.nan
        if rev_row and not pd.isna(rev_l) and rev_l != 0:
            net_margin = (ni_latest / rev_l) * 100

        # Annual NI series + stability (CoV)
        annual_ni = {}
        for d in dates_sorted:
            v = income_stmt.loc[ni_row, d]
            if not pd.isna(v):
                annual_ni[d.year] = v
        ni_vals_list = list(annual_ni.values())
        ni_cov = np.nan
        if len(ni_vals_list) >= 3:
            mu = np.mean(ni_vals_list)
            if mu != 0:
                ni_cov = abs(np.std(ni_vals_list, ddof=1) / mu)

        # =============================================================
        # BALANCE SHEET — ROIC + ROE
        # =============================================================
        roic, roe = np.nan, np.nan
        invested_capital = np.nan

        try:
            bs = stock.balance_sheet
            if bs is not None and not bs.empty:
                bsd = sorted(bs.columns)[-1]

                total_debt = 0.0
                for k in [
                    "Total Debt",
                    "Long Term Debt",
                    "Long Term Debt And Capital Lease Obligation",
                ]:
                    if k in bs.index:
                        v = bs.loc[k, bsd]
                        if not pd.isna(v):
                            total_debt = v
                            break

                st_debt = 0.0
                for k in [
                    "Current Debt",
                    "Current Debt And Capital Lease Obligation",
                ]:
                    if k in bs.index:
                        v = bs.loc[k, bsd]
                        if not pd.isna(v):
                            st_debt = v
                            break

                total_equity = np.nan
                for k in [
                    "Stockholders Equity",
                    "Total Equity Gross Minority Interest",
                    "Common Stock Equity",
                ]:
                    if k in bs.index:
                        total_equity = bs.loc[k, bsd]
                        break

                cash = 0.0
                for k in [
                    "Cash And Cash Equivalents",
                    "Cash Cash Equivalents And Short Term Investments",
                ]:
                    if k in bs.index:
                        v = bs.loc[k, bsd]
                        if not pd.isna(v):
                            cash = v
                            break

                if not pd.isna(total_equity):
                    invested_capital = total_equity + total_debt + st_debt - cash
                    if total_equity > 0 and not pd.isna(ni_latest):
                        roe = (ni_latest / total_equity) * 100

                # ROIC = NOPAT / Invested Capital
                oi_row = _find_row(income_stmt, ["Operating Income", "EBIT"])
                if (
                    oi_row
                    and not pd.isna(invested_capital)
                    and invested_capital > 0
                ):
                    oi = income_stmt.loc[oi_row, latest_date]
                    etr = 0.21
                    tr = _find_row(
                        income_stmt, ["Tax Provision", "Income Tax Expense"]
                    )
                    pr = _find_row(
                        income_stmt, ["Pretax Income", "Income Before Tax"]
                    )
                    if tr and pr:
                        tv = income_stmt.loc[tr, latest_date]
                        pv = income_stmt.loc[pr, latest_date]
                        if not pd.isna(tv) and not pd.isna(pv) and pv > 0:
                            etr = max(0.0, min(tv / pv, 0.5))
                    if not pd.isna(oi):
                        roic = (oi * (1 - etr) / invested_capital) * 100
        except Exception:
            pass

        # ROE fallback from yfinance info
        if pd.isna(roe):
            ri = _sg(info, "returnOnEquity")
            if not pd.isna(ri):
                roe = ri * 100

        # =============================================================
        # CASH FLOW
        # =============================================================
        fcf_l, fcf_e, fcf_cagr = np.nan, np.nan, np.nan
        cash_conv, fcf_yield = np.nan, np.nan

        try:
            cf = stock.cashflow
            if cf is not None and not cf.empty:
                cfd = sorted(cf.columns)
                fcf_r = _find_row(cf, ["Free Cash Flow"])
                ocf_r = _find_row(
                    cf,
                    [
                        "Operating Cash Flow",
                        "Cash Flow From Continuing Operating Activities",
                        "Total Cash From Operating Activities",
                    ],
                )
                cap_r = _find_row(
                    cf,
                    [
                        "Capital Expenditure",
                        "Capital Expenditures",
                        "Purchase Of PPE",
                    ],
                )

                def _fcf(dt):
                    if fcf_r:
                        v = cf.loc[fcf_r, dt]
                        if not pd.isna(v):
                            return v
                    if ocf_r:
                        o = cf.loc[ocf_r, dt]
                        c = 0.0
                        if cap_r:
                            cv = cf.loc[cap_r, dt]
                            if not pd.isna(cv):
                                c = cv
                        if not pd.isna(o):
                            return o + c if c <= 0 else o - c
                    return np.nan

                fcf_l = _fcf(cfd[-1])
                if len(cfd) >= 2:
                    fcf_e = _fcf(cfd[0])
                cfy = (
                    (cfd[-1] - cfd[0]).days / 365.25 if len(cfd) >= 2 else 0
                )
                fcf_cagr = _safe_cagr(fcf_e, fcf_l, cfy)
                if (
                    not pd.isna(fcf_l)
                    and not pd.isna(ni_latest)
                    and ni_latest > 0
                ):
                    cash_conv = _soft_cap_cc((fcf_l / ni_latest) * 100)
        except Exception:
            pass

        # =============================================================
        # MARKET CAP
        # =============================================================
        shares = _resolve_shares(info)
        if pd.isna(shares) or shares <= 0:
            _reject("shares_unavailable")
            return None

        hist = yf_download_with_retry(
            ticker,
            period=f"{lookback_years + 1}y",
            interval="1mo",
            silent=True,
        )
        if hist is None or hist.empty:
            _reject("hist_download_failed")
            return None

        ep = hist["Close"].iloc[0]
        lp = hist["Close"].iloc[-1]
        if pd.isna(ep) or pd.isna(lp):
            _reject("hist_price_NaN")
            return None

        mc_e = ep * shares
        mc_l = lp * shares
        mcy = (hist.index[-1] - hist.index[0]).days / 365.25
        if mcy < 0.5:
            _reject(f"mcap_span_short ({mcy:.2f}y)")
            return None
        if mc_e <= 0:
            _reject("mcap_earliest_<=0")
            return None

        mcap_cagr = ((mc_l / mc_e) ** (1 / mcy) - 1) * 100
        mcap_tg = ((mc_l - mc_e) / mc_e) * 100
        cur_mcap = _sg(info, "marketCap")
        if pd.isna(cur_mcap) or cur_mcap <= 0:
            cur_mcap = mc_l

        if not pd.isna(fcf_l) and cur_mcap > 0:
            fcf_yield = (fcf_l / cur_mcap) * 100

        # =============================================================
        # P/E (capped + floored)
        # =============================================================
        pc, pf = CONFIG["pe_cap"], CONFIG["pe_floor"]
        pe_s, pe_e, pe_exp = np.nan, np.nan, np.nan

        if ni_earliest > 0 and mc_e > 0:
            raw_pe = mc_e / ni_earliest
            pe_s = _cap(raw_pe, lo=pf, hi=pc) if raw_pe >= pf else np.nan
        if ni_latest > 0 and mc_l > 0:
            raw_pe = mc_l / ni_latest
            pe_e = _cap(raw_pe, lo=pf, hi=pc) if raw_pe >= pf else np.nan
        if not pd.isna(pe_s) and not pd.isna(pe_e) and pe_s > 0:
            pe_exp = ((pe_e / pe_s) ** (1 / year_span) - 1) * 100

        def _cap_pe(v):
            if pd.isna(v) or v < pf:
                return np.nan
            return min(v, pc)

        t_pe = _cap_pe(_sg(info, "trailingPE"))
        f_pe = _cap_pe(_sg(info, "forwardPE"))

        # =============================================================
        # FORWARD ESTIMATES + PEG
        # =============================================================
        feg = _sg(info, "earningsGrowth")
        frg = _sg(info, "revenueGrowth")
        eqg = _sg(info, "earningsQuarterlyGrowth")
        feg_p = feg * 100 if not pd.isna(feg) else np.nan
        frg_p = frg * 100 if not pd.isna(frg) else np.nan

        peg = np.nan
        if not pd.isna(f_pe) and not pd.isna(feg_p) and feg_p > 0:
            peg = f_pe / feg_p
        if (
            pd.isna(peg)
            and not pd.isna(t_pe)
            and not pd.isna(ni_cagr)
            and ni_cagr > 0
        ):
            peg = t_pe / ni_cagr
        peg = _cap(peg, lo=0, hi=CONFIG["peg_cap"])

        # =============================================================
        # ANNUAL + QUARTERLY SERIES FOR CHARTING
        # =============================================================
        annual_mcap = {}
        for yr in sorted(annual_ni.keys()):
            yd = hist.loc[hist.index.year == yr]
            if not yd.empty:
                annual_mcap[yr] = yd["Close"].iloc[-1] * shares

        annual_pe = {}
        for yr in sorted(annual_ni.keys()):
            nv = annual_ni[yr]
            mv = annual_mcap.get(yr)
            if nv and nv > 0 and mv and mv > 0:
                raw = mv / nv
                if raw >= pf:
                    annual_pe[yr] = min(raw, pc)

        q_ni, q_mc = {}, {}
        try:
            qs = stock.quarterly_income_stmt
            if qs is not None and not qs.empty:
                qr = _find_ni_row(qs)
                if qr:
                    for qd in sorted(qs.columns):
                        v = qs.loc[qr, qd]
                        if not pd.isna(v):
                            q_ni[qd] = v
                    for qd in sorted(q_ni.keys()):
                        diffs = abs(hist.index - qd)
                        mi = diffs.argmin()
                        if diffs[mi].days <= 45:
                            q_mc[qd] = hist["Close"].iloc[mi] * shares
        except Exception:
            pass

        # =============================================================
        # BUILD RECORD + CLASSIFY
        # =============================================================
        sector = get_sector(ticker, info)

        record = {
            "ticker": ticker,
            "company_name": info.get("shortName", ticker),
            "sector": sector,
            "is_turnaround": is_turnaround,
            "ni_earliest": ni_earliest,
            "ni_latest": ni_latest,
            "ni_cagr": ni_cagr,
            "ni_total_growth": ni_total_growth,
            "rev_earliest": rev_e,
            "rev_latest": rev_l,
            "rev_cagr": rev_cagr,
            "mcap_earliest": mc_e,
            "mcap_latest": mc_l,
            "mcap_cagr": mcap_cagr,
            "mcap_total_growth": mcap_tg,
            "year_span": year_span,
            "pe_start": pe_s,
            "pe_end": pe_e,
            "pe_expansion_cagr": pe_exp,
            "trailing_pe": t_pe,
            "forward_pe": f_pe,
            "peg_ratio": peg,
            "fcf_latest": fcf_l,
            "fcf_earliest": fcf_e,
            "fcf_cagr": fcf_cagr,
            "fcf_yield": fcf_yield,
            "cash_conversion": cash_conv,
            "roic": roic,
            "roe": roe,
            "net_margin": net_margin,
            "ni_stability_cov": ni_cov,
            "invested_capital": invested_capital,
            "fwd_earnings_growth_pct": feg_p,
            "fwd_revenue_growth_pct": frg_p,
            "earnings_quarterly_growth": (
                eqg * 100 if not pd.isna(eqg) else np.nan
            ),
            "annual_ni": annual_ni,
            "annual_mcap": annual_mcap,
            "annual_pe": annual_pe,
            "quarterly_ni": q_ni,
            "quarterly_mcap": q_mc,
            "current_mcap": cur_mcap,
        }
        record["flags"] = classify_stock(record)
        return record

    except Exception as e:
        _reject(f"unhandled: {type(e).__name__}: {str(e)[:60]}")
        return None


# ==============================================================================
# FORMATTING
# ==============================================================================

def fln(n):
    if pd.isna(n):
        return "N/A"
    a, s = abs(n), "-" if n < 0 else ""
    if a >= 1e12:
        return f"{s}${a/1e12:.2f}T"
    if a >= 1e9:
        return f"{s}${a/1e9:.2f}B"
    if a >= 1e6:
        return f"{s}${a/1e6:.2f}M"
    return f"{s}${a:,.0f}"


def fp(v):
    return "N/A" if pd.isna(v) else f"{v:+.1f}%"


def fr(v, d=1):
    return "N/A" if pd.isna(v) else f"{v:.{d}f}x"


def fn(v, d=1):
    return "N/A" if pd.isna(v) else f"{v:.{d}f}"


def ff(flags):
    return ",".join(flags) if flags else ""


# ==============================================================================
# TABLES
# ==============================================================================

def print_summary_table(records):
    rows = []
    for r in records:
        d = (
            r["ni_cagr"] - r["mcap_cagr"]
            if not pd.isna(r["ni_cagr"]) and not pd.isna(r["mcap_cagr"])
            else np.nan
        )
        rows.append({
            "Ticker": r["ticker"],
            "Company": r["company_name"][:25],
            "Sector": r.get("sector", "")[:16],
            "MCap CAGR": fp(r["mcap_cagr"]),
            "NI CAGR": fp(r["ni_cagr"]),
            "NI-MCap pp": fp(d),
            "Flags": ff(r.get("flags", [])),
        })
    df = pd.DataFrame(rows)
    print("\n" + "=" * 120)
    print(df.to_string(index=False))
    print("=" * 120 + "\n")
    return df


def print_valuation_table(records, title=""):
    rows = []
    for r in records:
        qm, ql = r.get("roic"), "ROIC"
        if pd.isna(qm) and "FINANCIAL" in r.get("flags", []):
            qm, ql = r.get("roe"), "ROE"
        rows.append({
            "Ticker": r["ticker"],
            "Sector": r.get("sector", "")[:16],
            "P/E End": fr(r.get("pe_end")),
            "Fwd P/E": fr(r.get("forward_pe")),
            "PEG": fn(r.get("peg_ratio")),
            "FCF Yld": fp(r.get("fcf_yield")),
            "CashConv": fp(r.get("cash_conversion")),
            ql: fp(qm),
            "NI CoV": fn(r.get("ni_stability_cov"), 2),
            "Fwd EPS": fp(r.get("fwd_earnings_growth_pct")),
            "Fwd Rev": fp(r.get("fwd_revenue_growth_pct")),
            "Flags": ff(r.get("flags", [])),
        })
    df = pd.DataFrame(rows)
    if title:
        print(f"\n{title}")
    print("=" * 165)
    print(df.to_string(index=False))
    print("=" * 165 + "\n")
    return df


def print_composite_table(sdf, title="", top_n=25):
    cols = [
        "Ticker", "Company", "Sector", "Flags",
        "Valuation Score", "Quality Score", "Growth Score", "Momentum Score",
        "Composite Score", "Rank",
    ]
    ex = [c for c in cols if c in sdf.columns]
    if title:
        print(f"\n{title}")
    print("=" * 150)
    print(sdf[ex].head(top_n).to_string(index=False))
    print("=" * 150 + "\n")


def print_rejection_summary():
    if not _REJECTION_REASONS:
        return
    total = sum(_REJECTION_REASONS.values())
    print("\n" + "-" * 60)
    print(f"  DATA QUALITY — {total} tickers rejected")
    print("-" * 60)
    for reason, count in _REJECTION_REASONS.most_common():
        print(f"   {reason:<45} {count:>4}  ({count/total*100:5.1f}%)")
    print("-" * 60 + "\n")


# ==============================================================================
# CHARTS
# ==============================================================================

def _finish(fig, path):
    plt.savefig(path, dpi=150, bbox_inches="tight")
    if CONFIG.get("show_plots"):
        plt.show()
    else:
        plt.close(fig)
    log.info(f"  Chart saved -> {path}")


def plot_company(r, od):
    yn = sorted(r["annual_ni"].keys())
    vn = [r["annual_ni"][y] for y in yn]
    ym = sorted(r["annual_mcap"].keys())
    vm = [r["annual_mcap"][y] for y in ym]
    if len(yn) < 2 and len(ym) < 2:
        return
    fig, a1 = plt.subplots(figsize=(10, 5))
    a1.plot(ym, [v / 1e9 for v in vm], marker="o", color="#1f77b4",
            linewidth=2.5, label="MCap ($B)")
    a1.set_xlabel("Fiscal Year")
    a1.set_ylabel("MCap ($B)", color="#1f77b4")
    a1.tick_params(axis="y", labelcolor="#1f77b4")
    a1.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    a2 = a1.twinx()
    nb = [v / 1e9 for v in vn]
    bc = ["#e74c3c" if v < 0 else "#2ca02c" for v in nb]
    a2.bar(yn, nb, alpha=0.45, color=bc, width=0.5, label="NI ($B)")
    a2.set_ylabel("NI ($B)", color="#2ca02c")
    a2.tick_params(axis="y", labelcolor="#2ca02c")
    fs = f"  [{ff(r.get('flags', []))}]" if r.get("flags") else ""
    plt.title(
        f"{r['company_name']} ({r['ticker']}){fs}\n"
        f"MCap: {fp(r['mcap_cagr'])} | NI: {fp(r['ni_cagr'])}",
        fontsize=11, fontweight="bold",
    )
    h1, l1 = a1.get_legend_handles_labels()
    h2, l2 = a2.get_legend_handles_labels()
    a1.legend(h1 + h2, l1 + l2, loc="upper left")
    fig.tight_layout()
    _finish(fig, os.path.join(od, f"{r['ticker']}_growth.png"))


def plot_quarterly(r, od):
    qn, qm = r.get("quarterly_ni", {}), r.get("quarterly_mcap", {})
    if len(qn) < 2 and len(qm) < 2:
        return
    nd = sorted(qn.keys())
    nv = [qn[d] / 1e9 for d in nd]
    md = sorted(qm.keys())
    mv = [qm[d] / 1e9 for d in md]
    fig, a1 = plt.subplots(figsize=(12, 5.5))
    if md:
        a1.plot(md, mv, marker="o", markersize=4, color="#1f77b4",
                linewidth=2, label="MCap ($B)")
    a1.set_xlabel("Quarter")
    a1.set_ylabel("MCap ($B)", color="#1f77b4")
    a1.tick_params(axis="y", labelcolor="#1f77b4")

    def _qf(x, p=None):
        dt = mdates.num2date(x)
        return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"

    a1.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=14))
    a1.xaxis.set_major_formatter(mticker.FuncFormatter(_qf))
    plt.setp(a1.xaxis.get_majorticklabels(), rotation=45, ha="right")
    a2 = a1.twinx()
    if nd:
        nn = mdates.date2num(nd)
        bc = ["#e74c3c" if v < 0 else "#2ca02c" for v in nv]
        a2.bar(nn, nv, width=50, alpha=0.5, color=bc, label="NI ($B)")
    a2.set_ylabel("NI ($B)", color="#2ca02c")
    a2.tick_params(axis="y", labelcolor="#2ca02c")
    plt.title(
        f"{r['company_name']} ({r['ticker']}) — Quarterly ({len(qn)} qtrs)",
        fontsize=11, fontweight="bold",
    )
    h1, l1 = a1.get_legend_handles_labels()
    h2, l2 = a2.get_legend_handles_labels()
    a1.legend(h1 + h2, l1 + l2, loc="upper left")
    fig.tight_layout()
    _finish(fig, os.path.join(od, f"{r['ticker']}_quarterly.png"))


def plot_pe_history(r, od):
    pd_data = r.get("annual_pe", {})
    if len(pd_data) < 2:
        return
    yrs = sorted(pd_data.keys())
    pvs = [pd_data[y] for y in yrs]
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(yrs, pvs, marker="s", color="#8e44ad", linewidth=2.5,
            markersize=8, label="P/E")
    pm = np.mean(pvs)
    ax.axhline(pm, color="#e67e22", linestyle="--", linewidth=1.5,
               label=f"Mean={pm:.1f}x")
    fpe = r.get("forward_pe")
    if not pd.isna(fpe):
        ax.axhline(fpe, color="#27ae60", linestyle=":", linewidth=1.5,
                   label=f"Fwd={fpe:.1f}x")
    ax.set_xlabel("Year")
    ax.set_ylabel("P/E")
    ax.legend()
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.set_title(
        f"{r['company_name']} ({r['ticker']}) — P/E History",
        fontsize=12, fontweight="bold",
    )
    fig.tight_layout()
    _finish(fig, os.path.join(od, f"{r['ticker']}_pe_history.png"))


def plot_comparison_bar(recs, od, suffix="", fn_name="comparison.png"):
    data = [
        {"Ticker": r["ticker"], "MCap": r["mcap_cagr"], "NI": r["ni_cagr"]}
        for r in recs
        if not pd.isna(r["ni_cagr"]) and not pd.isna(r["mcap_cagr"])
    ]
    if not data:
        return
    df = pd.DataFrame(data).sort_values("NI", ascending=False)
    fig, ax = plt.subplots(figsize=(max(10, len(df) * 0.8), 6))
    x = np.arange(len(df))
    w = 0.35
    ax.bar(x - w / 2, df["MCap"], w, label="MCap CAGR", color="#1f77b4",
           edgecolor="white")
    ax.bar(x + w / 2, df["NI"], w, label="NI CAGR", color="#2ca02c",
           edgecolor="white")
    ax.set_xticks(x)
    ax.set_xticklabels(df["Ticker"], rotation=45, ha="right", fontsize=9)
    ax.set_ylabel("CAGR (%)")
    ax.set_title(f"MCap vs NI CAGR {suffix}", fontsize=14, fontweight="bold")
    ax.legend()
    ax.axhline(0, color="grey", linewidth=0.8, linestyle="--")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    _finish(fig, os.path.join(od, fn_name))


def plot_scatter(recs, thresh, od):
    data = []
    for r in recs:
        if pd.isna(r["ni_cagr"]) or pd.isna(r["mcap_cagr"]):
            continue
        d = r["ni_cagr"] - r["mcap_cagr"]
        data.append({
            "T": r["ticker"], "M": r["mcap_cagr"], "N": r["ni_cagr"],
            "OP": d > thresh, "OV": d < -thresh,
        })
    if not data:
        return
    df = pd.DataFrame(data)
    fig, ax = plt.subplots(figsize=(11, 9))
    c = df.apply(
        lambda r: "#2ca02c" if r["OP"]
        else ("#e74c3c" if r["OV"] else "#95a5a6"),
        axis=1,
    )
    s = df.apply(lambda r: 80 if r["OP"] or r["OV"] else 30, axis=1)
    ax.scatter(df["M"], df["N"], c=c, s=s, alpha=0.7, edgecolors="white")
    for _, row in df[df["OP"]].iterrows():
        ax.annotate(
            row["T"], (row["M"], row["N"]),
            fontsize=8, fontweight="bold", color="#2ca02c",
            textcoords="offset points", xytext=(5, 5),
        )
    for _, row in df[df["OV"]].iterrows():
        ax.annotate(
            row["T"], (row["M"], row["N"]),
            fontsize=8, fontweight="bold", color="#e74c3c",
            textcoords="offset points", xytext=(5, -10),
        )
    lims = [
        min(ax.get_xlim()[0], ax.get_ylim()[0]),
        max(ax.get_xlim()[1], ax.get_ylim()[1]),
    ]
    ax.plot(lims, lims, "k--", alpha=0.3)
    ax.plot(lims, [l + thresh for l in lims], "--", color="#2ca02c", alpha=0.5)
    ax.plot(lims, [l - thresh for l in lims], "--", color="#e74c3c", alpha=0.5)
    ax.set_xlabel("MCap CAGR (%)")
    ax.set_ylabel("NI CAGR (%)")
    ax.set_title(
        f"NI vs MCap CAGR (±{thresh:.0f}pp)", fontsize=13, fontweight="bold"
    )
    ax.legend(
        ["NI=MCap", f"+{thresh}pp", f"-{thresh}pp"],
        fontsize=9, loc="upper left",
    )
    ax.grid(alpha=0.3)
    fig.tight_layout()
    _finish(fig, os.path.join(od, "sp500_growth_scatter.png"))


def plot_sector_pe(recs, od):
    pc = CONFIG["pe_cap"]
    pf = CONFIG["pe_floor"]
    data = [
        {"Sector": r.get("sector", "?"), "P/E": r["pe_end"]}
        for r in recs
        if not pd.isna(r.get("pe_end")) and pf < r["pe_end"] <= pc
    ]
    if len(data) < 10:
        return
    df = pd.DataFrame(data)
    order = df.groupby("Sector")["P/E"].median().sort_values().index.tolist()
    fig, ax = plt.subplots(figsize=(14, 7))
    sns.boxplot(
        data=df, x="Sector", y="P/E", order=order, ax=ax,
        palette="Set2", fliersize=3,
    )
    ax.set_xticklabels(
        ax.get_xticklabels(), rotation=45, ha="right", fontsize=9
    )
    ax.set_title("P/E by Sector", fontsize=14, fontweight="bold")
    ax.set_ylim(0, min(pc, df["P/E"].quantile(0.95) * 1.2))
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    _finish(fig, os.path.join(od, "sp500_sector_pe.png"))


def plot_fcf_vs_growth(recs, od):
    data = [
        {
            "T": r["ticker"],
            "F": r.get("fcf_yield"),
            "N": r.get("ni_cagr"),
            "S": r.get("sector", "?"),
        }
        for r in recs
        if not pd.isna(r.get("fcf_yield"))
        and not pd.isna(r.get("ni_cagr"))
        and abs(r["fcf_yield"]) < 50
        and abs(r["ni_cagr"]) < 200
    ]
    if len(data) < 10:
        return
    df = pd.DataFrame(data)
    secs = df["S"].unique()
    pal = dict(zip(secs, sns.color_palette("husl", len(secs))))
    fig, ax = plt.subplots(figsize=(12, 8))
    for s in secs:
        sub = df[df["S"] == s]
        ax.scatter(
            sub["N"], sub["F"], c=[pal[s]], label=s,
            alpha=0.7, edgecolors="white", s=50,
        )
    ax.axhline(0, color="grey", linewidth=0.8, linestyle="--")
    ax.axvline(0, color="grey", linewidth=0.8, linestyle="--")

    # --- Label top tickers (top 10% FCF yield OR top 10% NI CAGR) ---
    fcf_q90 = df["F"].quantile(0.90)
    ni_q90 = df["N"].quantile(0.90)
    for _, row in df.iterrows():
        if row["F"] > fcf_q90 or row["N"] > ni_q90:
            ax.annotate(
                row["T"], (row["N"], row["F"]),
                fontsize=7, alpha=0.85, fontweight="bold",
                textcoords="offset points", xytext=(4, 4),
            )

    ax.set_xlabel("NI CAGR (%)")
    ax.set_ylabel("FCF Yield (%)")
    ax.set_title(
        "FCF Yield vs Growth (top-right = best)",
        fontsize=13, fontweight="bold",
    )
    ax.legend(fontsize=7, loc="upper left", ncol=2, framealpha=0.8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    _finish(fig, os.path.join(od, "sp500_fcf_vs_growth.png"))


def plot_roic_pe(recs, od):
    from matplotlib.patches import Patch

    pc = CONFIG["pe_cap"]
    data = []
    for r in recs:
        rv = r.get("roic")
        if pd.isna(rv) and "FINANCIAL" in r.get("flags", []):
            rv = r.get("roe")
        pe = r.get("pe_end")
        if pd.isna(rv) or pd.isna(pe) or pe <= 0 or pe > pc:
            continue
        if rv < -50 or rv > 200:
            continue
        data.append({"T": r["ticker"], "R": rv, "P": pe})
    if len(data) < 10:
        return
    df = pd.DataFrame(data)
    fig, ax = plt.subplots(figsize=(12, 8))

    def _qc(r):
        if r["R"] > 15 and r["P"] < 25:
            return "#2ca02c"
        if r["R"] > 15:
            return "#f39c12"
        if r["P"] < 25:
            return "#3498db"
        return "#e74c3c"

    ax.scatter(
        df["P"], df["R"], c=df.apply(_qc, axis=1),
        s=50, alpha=0.7, edgecolors="white",
    )
    ax.axhline(15, color="#888", linewidth=1, linestyle=":")
    ax.axvline(25, color="#888", linewidth=1, linestyle=":")

    # --- Label top tickers (top 10% ROIC OR high-ROIC + cheap P/E) ---
    roic_q90 = df["R"].quantile(0.90)
    for _, row in df.iterrows():
        if row["R"] > roic_q90 or (row["R"] > 20 and row["P"] < 15):
            ax.annotate(
                row["T"], (row["P"], row["R"]),
                fontsize=7, alpha=0.85, fontweight="bold",
                textcoords="offset points", xytext=(4, 4),
            )

    ax.legend(
        handles=[
            Patch(facecolor="#2ca02c", label="High Q + Fair P"),
            Patch(facecolor="#f39c12", label="High Q + Expensive"),
            Patch(facecolor="#3498db", label="Low Q + Cheap"),
            Patch(facecolor="#e74c3c", label="Low Q + Expensive"),
        ],
        fontsize=9, loc="upper right",
    )
    ax.set_xlabel("P/E")
    ax.set_ylabel("ROIC/ROE (%)")
    ax.set_title(
        "Quality vs Valuation (green quadrant = best)",
        fontsize=13, fontweight="bold",
    )
    ax.grid(alpha=0.3)
    fig.tight_layout()
    _finish(fig, os.path.join(od, "sp500_roic_pe.png"))


def plot_pe_decomp(recs, od, top_n=20):
    data = [
        {
            "T": r["ticker"],
            "NI": r["ni_cagr"],
            "PE": r["pe_expansion_cagr"],
            "MC": r["mcap_cagr"],
        }
        for r in recs
        if not any(
            pd.isna(r.get(k))
            for k in ["ni_cagr", "pe_expansion_cagr", "mcap_cagr"]
        )
    ]
    if not data:
        return
    df = pd.DataFrame(data).sort_values("MC", ascending=False).head(top_n)
    fig, ax = plt.subplots(figsize=(max(12, top_n * 0.6), 6))
    x = np.arange(len(df))
    w = 0.35
    ax.bar(
        x - w / 2, df["NI"], w, label="Earnings CAGR",
        color="#2ca02c", edgecolor="white",
    )
    ax.bar(
        x + w / 2, df["PE"], w, label="P/E Expansion",
        color="#e74c3c", edgecolor="white",
    )
    ax.plot(
        x, df["MC"].values, "ko-", markersize=5, linewidth=1.5,
        label="MCap CAGR",
    )
    ax.set_xticks(x)
    ax.set_xticklabels(df["T"], rotation=45, ha="right", fontsize=9)
    ax.set_ylabel("CAGR (%)")
    ax.set_title(
        "MCap Decomposition", fontsize=13, fontweight="bold"
    )
    ax.legend()
    ax.axhline(0, color="grey", linewidth=0.8, linestyle="--")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    _finish(fig, os.path.join(od, "sp500_pe_decomp.png"))


def plot_composite_bar(sdf, od, top_n=25):
    if sdf.empty:
        return
    df = sdf.head(top_n).copy()
    cols = [
        c for c in
        ["Valuation Score", "Quality Score", "Growth Score", "Momentum Score"]
        if c in df.columns
    ]
    if not cols:
        return
    cm = {
        "Valuation Score": "#3498db",
        "Quality Score": "#2ecc71",
        "Growth Score": "#e67e22",
        "Momentum Score": "#9b59b6",
    }
    fig, ax = plt.subplots(figsize=(max(12, top_n * 0.6), 7))
    x = np.arange(len(df))
    bot = np.zeros(len(df))
    for c in cols:
        v = df[c].fillna(0).values.astype(float)
        ax.bar(
            x, v, bottom=bot, label=c.replace(" Score", ""),
            color=cm.get(c, "#95a5a6"), edgecolor="white", width=0.7,
        )
        bot += np.maximum(v, 0)
    ax.set_xticks(x)
    ax.set_xticklabels(df["Ticker"], rotation=45, ha="right", fontsize=9)
    ax.set_ylabel("Score")
    ax.set_title(
        f"Top {top_n} Composite", fontsize=14, fontweight="bold"
    )
    ax.legend(fontsize=10, loc="upper right")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    _finish(fig, os.path.join(od, "sp500_composite_bar.png"))


# ==============================================================================
# COMPOSITE SCORING
# ==============================================================================

def _zs(s, hib=True):
    if s.dropna().count() < 5:
        return pd.Series(np.nan, index=s.index)
    r = s.rank(pct=True, na_option="keep") * 100
    return r if hib else 100 - r


def _sector_zs(vals, secs, hib=True):
    result = pd.Series(np.nan, index=vals.index)
    univ = _zs(vals, hib)
    for sec in secs.unique():
        m = secs == sec
        sv = vals[m]
        if sv.dropna().count() >= 5:
            r = sv.rank(pct=True, na_option="keep") * 100
            if not hib:
                r = 100 - r
            result[m] = r
    return result.fillna(univ)


def compute_composite_scores(records):
    if len(records) < 5:
        log.warning(
            f"Only {len(records)} stocks — need 5+ for composite scoring."
        )
        return pd.DataFrame()

    w = CONFIG["composite_weights"]
    rows = []
    for r in records:
        diff = np.nan
        if not pd.isna(r.get("ni_cagr")) and not pd.isna(r.get("mcap_cagr")):
            diff = r["ni_cagr"] - r["mcap_cagr"]

        # Turnaround: proxy with rev/fcf CAGR
        eff_ni = r.get("ni_cagr")
        if pd.isna(eff_ni) and r.get("is_turnaround"):
            eff_ni = r.get("rev_cagr")
            if pd.isna(eff_ni):
                eff_ni = r.get("fcf_cagr")

        # Quality: ROIC or ROE for financials
        qr = r.get("roic")
        if pd.isna(qr) and "FINANCIAL" in r.get("flags", []):
            qr = r.get("roe")

        rows.append({
            "Ticker": r["ticker"],
            "Company": r["company_name"][:25],
            "Sector": r.get("sector", "?"),
            "Flags": ff(r.get("flags", [])),
            "pe_end": r.get("pe_end"),
            "forward_pe": r.get("forward_pe"),
            "peg_ratio": r.get("peg_ratio"),
            "fcf_yield": r.get("fcf_yield"),
            "cash_conversion": r.get("cash_conversion"),
            "quality_return": qr,
            "ni_stability_cov": r.get("ni_stability_cov"),
            "ni_cagr": eff_ni,
            "rev_cagr": r.get("rev_cagr"),
            "fwd_eg": r.get("fwd_earnings_growth_pct"),
            "fwd_rg": r.get("fwd_revenue_growth_pct"),
            "mcap_cagr": r.get("mcap_cagr"),
            "ni_mcap_diff": diff,
            "net_margin": r.get("net_margin"),
            # Display columns
            "MCap CAGR": fp(r.get("mcap_cagr")),
            "NI CAGR": fp(r.get("ni_cagr")),
            "P/E": fr(r.get("pe_end")),
            "Fwd P/E": fr(r.get("forward_pe")),
            "FCF Yield": fp(r.get("fcf_yield")),
            "ROIC": fp(qr),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # VALUATION (sector-relative P/E, forward-weighted)
    pe_s = df["pe_end"].copy()
    pe_s[pe_s <= 0] = np.nan
    fpe_s = df["forward_pe"].copy()
    fpe_s[fpe_s <= 0] = np.nan
    peg_s = df["peg_ratio"].copy()
    peg_s[peg_s <= 0] = np.nan

    df["Valuation Score"] = (
        _sector_zs(pe_s, df["Sector"], False).fillna(50) * 0.15
        + _sector_zs(fpe_s, df["Sector"], False).fillna(50) * 0.35
        + _zs(peg_s, False).fillna(50) * 0.25
        + _zs(df["fcf_yield"], True).fillna(50) * 0.25
    )

    # QUALITY
    df["Quality Score"] = (
        _zs(df["quality_return"], True).fillna(50) * 0.35
        + _zs(df["cash_conversion"], True).fillna(50) * 0.25
        + _zs(df["ni_stability_cov"], False).fillna(50) * 0.20
        + _zs(df["net_margin"], True).fillna(50) * 0.20
    )

    # GROWTH (4-metric: NI + Rev + Fwd EPS + Fwd Rev)
    df["Growth Score"] = (
        _zs(df["ni_cagr"], True).fillna(50) * 0.30
        + _zs(df["rev_cagr"], True).fillna(50) * 0.25
        + _zs(df["fwd_eg"], True).fillna(50) * 0.25
        + _zs(df["fwd_rg"], True).fillna(50) * 0.20
    )

    # MOMENTUM
    df["Momentum Score"] = _zs(df["ni_mcap_diff"], True).fillna(50)

    # COMPOSITE
    df["Composite Score"] = (
        df["Valuation Score"] * w["valuation"]
        + df["Quality Score"] * w["quality"]
        + df["Growth Score"] * w["growth"]
        + df["Momentum Score"] * w["momentum"]
    )

    for c in [
        "Valuation Score", "Quality Score", "Growth Score",
        "Momentum Score", "Composite Score",
    ]:
        df[c] = df[c].round(1)

    df = df.sort_values("Composite Score", ascending=False).reset_index(drop=True)
    df["Rank"] = range(1, len(df) + 1)
    return df


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    t0 = datetime.now()
    od = CONFIG["output_dir"]
    ensure_output_dir(od)
    doq = CONFIG["plot_quarterly"]
    sleeptime = CONFIG["per_ticker_sleep"]

    try:
        plt.style.use(CONFIG["chart_style"])
    except Exception:
        plt.style.use("ggplot")

    # =====================================================
    # PART 1 — Configured tickers
    # =====================================================
    print("\n" + "=" * 60)
    print("  PART 1 — Configured tickers")
    print("=" * 60)

    user_recs = []
    for tk in CONFIG["tickers"]:
        log.info(f"Fetching {tk}…")
        rec = fetch_growth_data(tk, CONFIG["lookback_years"])
        if rec:
            user_recs.append(rec)
            _FETCHED_RECORDS_CACHE[tk] = rec
            log.info(
                f"  OK {rec['company_name']}: "
                f"MCap={fp(rec['mcap_cagr'])}, NI={fp(rec['ni_cagr'])}, "
                f"ROIC={fp(rec.get('roic'))}, ROE={fp(rec.get('roe'))}, "
                f"FCFy={fp(rec.get('fcf_yield'))}, flags={rec.get('flags', [])}"
            )
        else:
            log.warning(f"  SKIP {tk}")
        time.sleep(sleeptime)

    if user_recs:
        print_summary_table(user_recs)
        print_valuation_table(user_recs, title="Configured — Valuation")
        for r in user_recs:
            plot_company(r, od)
            plot_pe_history(r, od)
            if doq:
                plot_quarterly(r, od)
        plot_comparison_bar(
            user_recs, od, "(Configured)", "configured_comparison.png"
        )
        if len(user_recs) >= 5:
            scored = compute_composite_scores(user_recs)
            if not scored.empty:
                print_composite_table(scored, title="Configured — Composite")
        elif len(user_recs) >= 2:
            log.info(
                f"  {len(user_recs)} stocks — need 5+ for composite scoring."
            )
    else:
        log.warning("No valid data for configured tickers.")

    if _REJECTION_REASONS:
        print_rejection_summary()
        _REJECTION_REASONS.clear()

    # =====================================================
    # PART 2 — S&P 500
    # =====================================================
    if not CONFIG["run_sp500_scan"]:
        return

    cooldown = CONFIG["inter_part_cooldown"]
    if cooldown > 0:
        log.info(f"Cooling down {cooldown}s…")
        time.sleep(cooldown)

    print("\n" + "=" * 60)
    print("  PART 2 — S&P 500 Full Scan")
    print("=" * 60)

    sp500 = get_sp500_tickers()
    if not sp500:
        log.error("Could not get S&P 500 list.")
        return

    thresh = CONFIG["outperformance_threshold_pp"]
    all_recs, ops, ovs = [], [], []

    for tk in tqdm(sp500, desc="Scanning S&P 500", unit="stock"):
        # Use cache from Part 1 if available
        if tk in _FETCHED_RECORDS_CACHE:
            rec = _FETCHED_RECORDS_CACHE[tk]
        else:
            rec = fetch_growth_data(tk, CONFIG["lookback_years"])
            time.sleep(sleeptime)

        if rec is None:
            continue
        all_recs.append(rec)
        if pd.isna(rec["ni_cagr"]) or pd.isna(rec["mcap_cagr"]):
            continue
        d = rec["ni_cagr"] - rec["mcap_cagr"]
        if d > thresh:
            ops.append(rec)
        elif d < -thresh:
            ovs.append(rec)

    pct = len(all_recs) / len(sp500) * 100 if sp500 else 0
    log.info(f"\nValid: {len(all_recs)}/{len(sp500)} ({pct:.1f}%)")
    log.info(f"Outperformers: {len(ops)}  |  Overvalued: {len(ovs)}\n")
    print_rejection_summary()

    # 2A: Outperformers
    print(
        f"\n{'-' * 60}\n"
        f"  PART 2A — Outperformers (>{thresh}pp)\n"
        f"{'-' * 60}"
    )
    if ops:
        ops.sort(
            key=lambda r: r["ni_cagr"] - r["mcap_cagr"], reverse=True
        )
        df_out = print_summary_table(ops)
        print_valuation_table(ops, title="Outperformers — Valuation")
        df_out.to_csv(
            os.path.join(od, "sp500_outperformers.csv"), index=False
        )
        plot_comparison_bar(
            ops, od, f"(OP>{thresh}pp)", "sp500_outperformers_bar.png"
        )
        for r in ops[:10]:
            plot_company(r, od)
            plot_pe_history(r, od)
            if doq:
                plot_quarterly(r, od)
    else:
        log.info("No outperformers found.")

    # 2B: Overvalued
    print(
        f"\n{'-' * 60}\n"
        f"  PART 2B — Overvalued (>{thresh}pp)\n"
        f"{'-' * 60}"
    )
    if ovs:
        ovs.sort(
            key=lambda r: r["mcap_cagr"] - r["ni_cagr"], reverse=True
        )
        df_ov = print_summary_table(ovs)
        print_valuation_table(ovs, title="Overvalued — Valuation")
        df_ov.to_csv(
            os.path.join(od, "sp500_overvalued.csv"), index=False
        )
        plot_comparison_bar(
            ovs, od, f"(OV>{thresh}pp)", "sp500_overvalued_bar.png"
        )
        for r in ovs[:10]:
            plot_company(r, od)
            plot_pe_history(r, od)
            if doq:
                plot_quarterly(r, od)
    else:
        log.info("No overvalued found.")

    plot_scatter(all_recs, thresh, od)

    # =====================================================
    # PART 3 — Dashboard
    # =====================================================
    if not CONFIG["run_valuation_dashboard"] or len(all_recs) < 20:
        elapsed = (datetime.now() - t0).total_seconds()
        print(f"\nDone in {elapsed:.1f}s. Output -> ./{od}/\n")
        return

    print(
        f"\n{'=' * 60}\n"
        f"  PART 3 — Valuation Dashboard\n"
        f"{'=' * 60}"
    )

    tn = CONFIG["dashboard_top_n"]
    plot_sector_pe(all_recs, od)
    plot_pe_decomp(all_recs, od, tn)
    plot_fcf_vs_growth(all_recs, od)
    plot_roic_pe(all_recs, od)

    sdf = compute_composite_scores(all_recs)
    if not sdf.empty:
        print_composite_table(
            sdf, title="S&P 500 — Top Composite", top_n=tn
        )

        csv_cols = [
            "Rank", "Ticker", "Company", "Sector", "Flags",
            "MCap CAGR", "NI CAGR", "P/E", "Fwd P/E",
            "FCF Yield", "ROIC",
            "Valuation Score", "Quality Score", "Growth Score",
            "Momentum Score", "Composite Score",
        ]
        ex = [c for c in csv_cols if c in sdf.columns]
        sdf[ex].to_csv(
            os.path.join(od, "sp500_composite_ranking.csv"), index=False
        )
        log.info("  CSV saved -> sp500_composite_ranking.csv")

        try:
            xp = os.path.join(od, "sp500_dashboard.xlsx")
            with pd.ExcelWriter(xp, engine="openpyxl") as wr:
                sdf[ex].to_excel(
                    wr, sheet_name="Composite", index=False
                )
                if ops:
                    pd.DataFrame([
                        {
                            "Ticker": r["ticker"],
                            "Company": r["company_name"],
                            "Sector": r.get("sector"),
                            "Flags": ff(r.get("flags", [])),
                            "MCap CAGR": r["mcap_cagr"],
                            "NI CAGR": r["ni_cagr"],
                            "P/E": r.get("pe_end"),
                            "FCF Yield": r.get("fcf_yield"),
                            "ROIC": r.get("roic"),
                            "ROE": r.get("roe"),
                            "PEG": r.get("peg_ratio"),
                        }
                        for r in ops
                    ]).to_excel(wr, sheet_name="Outperformers", index=False)
                if ovs:
                    pd.DataFrame([
                        {
                            "Ticker": r["ticker"],
                            "Company": r["company_name"],
                            "Sector": r.get("sector"),
                            "Flags": ff(r.get("flags", [])),
                            "MCap CAGR": r["mcap_cagr"],
                            "NI CAGR": r["ni_cagr"],
                            "P/E": r.get("pe_end"),
                            "FCF Yield": r.get("fcf_yield"),
                            "ROIC": r.get("roic"),
                            "ROE": r.get("roe"),
                            "PEG": r.get("peg_ratio"),
                        }
                        for r in ovs
                    ]).to_excel(wr, sheet_name="Overvalued", index=False)
            log.info(f"  Excel saved -> {xp}")
        except Exception as e:
            log.warning(f"  Excel failed: {e}")

        plot_composite_bar(sdf, od, tn)

        bot = sdf.sort_values("Composite Score").head(tn).copy()
        bot["Rank"] = range(len(sdf), len(sdf) - tn, -1)
        print_composite_table(
            bot, title="S&P 500 — Bottom Composite", top_n=tn
        )

    # =====================================================
    # DONE
    # =====================================================
    elapsed = (datetime.now() - t0).total_seconds()
    print(f"\nDone in {elapsed:.1f}s. Output -> ./{od}/\n")

    files = sorted(os.listdir(od))
    print(f"Output files ({len(files)}):")
    for f in files:
        sz = os.path.getsize(os.path.join(od, f))
        if sz < 1e6:
            print(f"   {f:<55} {sz / 1024:.1f} KB")
        else:
            print(f"   {f:<55} {sz / 1e6:.1f} MB")
    print()


if __name__ == "__main__":
    main()
