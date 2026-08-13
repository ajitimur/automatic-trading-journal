# Daily OHLCV sources for US and IDX

Research for [#3](https://github.com/ajitimur/automatic-trading-journal/issues/3) (part of the map, [#1](https://github.com/ajitimur/automatic-trading-journal/issues/1)).

- **Date of research: 2026-08-12.** Every price, rate limit and terms quote below was read on that date. Vendor pricing and API surfaces change; re-check before committing money.
- **Method:** primary sources only — vendor documentation, pricing pages, API references, licence pages. Where a claim could not be traced to a vendor-owned page, it is marked **UNVERIFIED**.
- Several claims are backed by **empirical probes** run against the vendor's own live API on 2026-08-12 (script in the appendix). Those are labelled *(probe)*.

---

## TL;DR — ranked recommendation

### US equities

| Rank | Source | Cost | Why |
|---|---|---|---|
| **1** | **yfinance → Yahoo Finance** | $0 | Full history (AAPL from 1980-12-12, *probe*), split-adjusted OHLC + dividend-adjusted close + the split/dividend event series in one call. Zero cost, zero account, no data-retention clause. **Terms are the problem** — see caveats. |
| 2 | EODHD "EOD Historical Data" plan | $19.99/mo, $199/yr | The clean-licence fallback. Explicitly separates raw OHLC from adjusted close; `delisted=1` ticker listing. |
| 3 | IBKR TWS/Web API (`ADJUSTED_LAST` / `TRADES`) | $10/mo non-pro market-data bundle | Same broker as the trades, so no symbology mismatch. Rate-limited hard and needs a running gateway. |
| 4 | Massive (ex-Polygon.io) | $29–$199/mo | Excellent US data, but history depth is tiered by price and it is US-only, so it cannot be the single pipeline. |
| 5 | Tiingo | $0–$30/mo | Cheap and clean, but US/China only. |

### IDX equities

| Rank | Source | Cost | Why |
|---|---|---|---|
| **1** | **yfinance → Yahoo Finance (`.JK`)** | $0 | Verified working for large-, mid- and small-cap IDX names, with splits and dividends, and both IHSG (`^JKSE`) and LQ45 (`^JKLQ45`) *(probe)*. Nothing else comes close on price/coverage. |
| 2 | EODHD (`JK` exchange, MIC `XIDX`) | $19.99/mo | 962 active JK tickers listed by the vendor; the only paid source with a *documented, licensed* Indonesian EOD feed at retail price. |
| 3 | Twelve Data Pro | $99/mo | XIDX is covered but only from the Pro tier — 5× the EODHD price for the same market. |
| 4 | Niche IDX vendors (OHLC.dev, Invezgo, iTick) | UNVERIFIED | Carry IDX, but none documents adjustment policy or delisted coverage, and none is an IDX-authorised distributor as far as their own pages state. Key-man risk on a journal meant to outlive the year. |
| — | **IBKR** | — | **Impossible.** IBKR does not offer Indonesian equities at all (see §5). |
| — | **Stockbit** | — | **No public developer API found** (see §2.4). |

**Recommendation: one source, both markets — yfinance.** It is the only candidate that covers US *and* IDX *and* both benchmark index families with one symbology and one code path, at zero cost. EODHD is the pre-identified escape hatch: it is the only alternative that also spans both markets, and switching costs one adapter.

---

## 1. What the source actually has to do

From the map (#1), the enrichment pipeline needs, per trade:

- Daily bars covering **entry − ~200 trading days** (MA200 needs 200 prior closes) through **exit + 20 trading days**.
- **Arbitrary past dates** — manual backdated entry is first-class, so "last 2 years of history" tiers are disqualifying.
- A **daily background job** — so the source must be callable unattended, without a human-driven login.
- Both markets, and index series for regime classification.

Two consequences that drive the whole comparison:

1. **History depth is a hard requirement, not a nice-to-have.** Any plan that tiers history depth (Massive: 2/5/10/20+ years by price; Marketstack: 1/10/15+ years by price) is priced against the one thing this project cannot compromise on.
2. **The job runs headless.** This is what demotes IBKR: its API requires an authenticated gateway session, not a bearer token.

---

## 2. Candidate sources

### 2.1 yfinance / Yahoo Finance — RANKED #1, BOTH MARKETS

**What it is.** `yfinance` is an open-source Python package (Apache Software Licence) that reads Yahoo Finance's public endpoints. It is explicitly *not* an official API: "yfinance is **not** affiliated, endorsed, or vetted by Yahoo, Inc. It's an open-source tool that uses Yahoo's publicly available APIs, and is intended for research and educational purposes." — <https://ranaroussi.github.io/yfinance/index.html> (2026-08-12).

**Cost.** $0. No account, no key.

**Coverage and history depth (probe, yfinance 1.5.2, 2026-08-12).** `Ticker.history(period="max", interval="1d", auto_adjust=False, actions=True)`:

| Symbol | Rows | First bar | Last bar | Splits seen | Divs seen |
|---|---|---|---|---|---|
| AAPL | 11,507 | 1980-12-12 | 2026-08-11 | 5 | 92 |
| SPY | 8,440 | 1993-01-29 | 2026-08-11 | 0 | 135 |
| QQQ | 6,898 | 1999-03-10 | 2026-08-11 | 1 | 89 |
| ^GSPC | 24,769 | 1927-12-30 | 2026-08-11 | — | — |
| ^NDX | 10,294 | 1985-10-01 | 2026-08-11 | — | — |
| BBCA.JK | 5,469 | 2004-06-08 | 2026-08-12 | 2 | 46 |
| BBRI.JK | 5,620 | 2003-11-10 | 2026-08-12 | 2 | 29 |
| TLKM.JK | 5,388 | 2004-09-28 | 2026-08-12 | 1 | 29 |
| ANTM.JK | 5,137 | 2005-09-29 | 2026-08-12 | 1 | 18 |
| PTBA.JK | 5,849 | 2002-12-23 | 2026-08-12 | 1 | 28 |
| MDKA.JK | 2,735 | 2015-06-19 | 2026-08-12 | 1 | 1 |
| GOTO.JK | 1,038 | 2022-04-11 | 2026-08-12 | 0 | 0 |
| BREN.JK | 678 | 2023-10-09 | 2026-08-12 | 0 | 4 |
| ^JKSE (IHSG) | 8,847 | 1990-04-06 | 2026-08-12 | — | — |
| ^JKLQ45 | 7,155 | 1997-02-24 | 2026-08-12 | — | — |

Notes on the IDX rows:

- Coverage is **complete for the horizon this journal needs** (a swing trader's backdated entries), but **IDX history effectively begins ~2002–2005**, not at the listing date. TLKM listed in 1995; Yahoo's series starts 2004-09-28. Do not treat `.JK` first-bar dates as IPO dates.
- Newly listed names appear promptly (BREN listed Oct 2023, first bar 2023-10-09).
- Data quality on recent IDX bars looked clean: BBCA.JK 2026 YTD had 147 rows, **0 NaNs** in OHLCV, and 4 zero-volume days (consistent with suspension/no-trade days rather than gaps).

**Rate limits.** **UNVERIFIED — no documented limit exists**, because there is no public Yahoo API contract to document one. Empirically, a plain `curl` to `query1.finance.yahoo.com/v8/finance/chart/BBCA.JK` returned **HTTP 429 "Too Many Requests"** on the first attempt (2026-08-12), while yfinance's own session (which negotiates cookie + crumb) succeeded for 21 symbols back-to-back. **The library's session handling is load-bearing**; a hand-rolled HTTP client against Yahoo is not a viable substitute. Plan for retry/backoff and an on-disk bar cache regardless.

**Licence — the real caveat.** Yahoo's Terms of Service prohibit exactly what this project would do. Verbatim from <https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html> (2026-08-12):

> "access or collect data, or attempt to access or collect data, from our Services using any automated means, devices, programs, algorithms or methodologies, including but not limited to robots, spiders, scrapers, data mining tools, or data gathering or extraction tools, for any purpose without our express, prior permission."

and

> "Unless otherwise expressly stated, you may not access or reuse the Services, or any portion thereof, for any commercial purpose."

yfinance's own docs point at this and note the Yahoo finance API is "intended for personal use only" (<https://ranaroussi.github.io/yfinance/index.html>).

**Assessment.** The commercial-use bar is not breached by a single-user private journal. The **automated-access** bar is breached by any scripted use, including this one — that is a terms question, not a technical one, and it applies to every yfinance user. It is a live risk to accept knowingly, not a technicality to wave off: the exposure is *service withdrawal or blocking*, not liability, and the mitigation is (a) polite request rates, (b) a local bar cache so the journal keeps working if the feed dies, and (c) a pre-built EODHD adapter behind the same interface.

### 2.2 EODHD — RANKED #2, BOTH MARKETS (the escape hatch)

**Pricing** (<https://eodhd.com/pricing>, 2026-08-12):

| Plan | Monthly | Yearly | Calls/day | Req/min |
|---|---|---|---|---|
| Free | $0 | $0 | 20 | 20 |
| **EOD Historical Data** | **$19.99** | **$199.00** | 100,000 | 1,000 |
| EOD + Intraday | $29.99 | $299.90 | 100,000 | 1,000 |
| ALL-IN-ONE | $99.99 | $999.90 | 100,000 | 1,000 |

The **$19.99 EOD plan is sufficient** — this project needs daily bars, splits, dividends and indices, nothing intraday and no fundamentals.

**History depth** (<https://eodhd.com/pricing>): "US Stocks, ETFs, Mutual Funds: from earliest available (e.g. Ford Motors from Jun 1972)"; "**Non-US Exchanges: mostly from Jan 3, 2000**". The non-US start date is *better* than Yahoo's effective IDX start (~2002–2005), which is a point in EODHD's favour for deep backdating.

**IDX coverage.** Exchange code `JK`, MIC `XIDX`, **962 active tickers**, currency IDR — <https://eodhd.com/exchange/JK> (2026-08-12). The per-exchange page does **not** state a JK history start date; the "mostly from Jan 3, 2000" line on the pricing page is the only vendor claim available. **Partially unverified** — confirm on a free-tier probe before switching.

**Adjustment.** Best-documented of any candidate (<https://eodhd.com/financial-apis/api-for-historical-data-and-volumes>):
- OHLC values are "raw — adjusted for neither splits nor dividends"
- `adjusted_close` is adjusted "for both splits and dividends"
- Volume is adjusted for splits only
- Split-only-adjusted OHLC is available via the Technical API with `function=splitadjusted`

**This is the only candidate that offers a genuinely unadjusted OHLC series** — see §3.

**Delisted tickers.** Supported: `delisted=1` on the ticker-list endpoint; by default "only tickers that have been active in the past month are included" (<https://eodhd.com/financial-apis/list-supported-exchanges>).

**Licence.** Non-Professional Users may store, manipulate and analyse the data for private, non-commercial purposes, but may not sell, resell, retransmit, redistribute or display it. **Critical retention clause:** data may be stored on the subscriber's premises during the active subscription, but on termination or expiry the subscriber must **delete all copies within one (1) month** (<https://eodhd.com/financial-apis/terms-conditions>). For this project that means a cached bar store is contractually a *rented* asset — but the derived enrichment fields written into the trade record are a separate question the terms do not clearly address. **Flag for #7/#9.**

### 2.3 Twelve Data — RANKED #3 for IDX

- XIDX (Indonesia Stock Exchange) is covered, listed under **Pro tier and above**, session 09:00–15:00 Asia/Jakarta (<https://twelvedata.com/stocks>, 2026-08-12).
- Pricing (<https://twelvedata.com/pricing>, 2026-08-12): Basic free (8 credits/min, 800/day), Grow $29/mo (55/min), **Pro $99/mo (610/min)**, Ultra $329/mo (2,584/min). Non-US markets start at Pro.
- Individual plans are stated to be for "personal, internal, and non-commercial purposes".
- **History depth per exchange: UNVERIFIED** — not stated on the pricing page.
- **Verdict:** functionally viable for IDX, but $99/mo against EODHD's $19.99 for the same market, with worse-documented adjustment semantics. No reason to prefer it.

### 2.4 Stockbit — NOT VIABLE AS A DATA SOURCE

Stockbit is the IDX *broker* in this project, so it is the natural analogue to IBKR on the US side. **No public developer API or developer documentation was found.** `https://stockbit.com/developer` returns HTTP 200 but is a **hiring page** (page title: "Developer (Developer) | Stockbit"), not an API portal (checked 2026-08-12). Web search surfaced several pages purporting to document a "Stockbit Public API", but all were on unrelated third-party domains with hallmarks of auto-generated SEO content — **no primary source**, so they are excluded under the method.

**Conclusion: Stockbit is a statement source, not a bar source.** Marked **UNVERIFIED** rather than "does not exist" — a private/partner API may exist behind login.

### 2.5 Others screened and rejected

| Source | Verdict | Evidence (all 2026-08-12) |
|---|---|---|
| **Massive** (ex-Polygon.io; `polygon.io/pricing` 301-redirects to `massive.com/pricing`) | US-only. History tiered by price: Basic free = 2 yr, Starter $29 = 5 yr, Developer $79 = 10 yr, Advanced $199 = 20+ yr. "Individual use only". Paying $199/mo for the history depth this project assumes, and still needing a second IDX source, is the wrong shape. | <https://massive.com/pricing> |
| **Tiingo** | US + Chinese stocks only; no Indonesia. Starter free (50 req/hr, 1,000/day, 500 symbols/mo), Power $30/mo (10,000 req/hr, 100,000/day). | <https://www.tiingo.com/pricing> |
| **Alpha Vantage** | Free tier is **25 requests/day** — unusable for a daily job over a portfolio. Premium $49.99–$249.99/mo for 75–1,200 req/min. Overpriced for daily bars; IDX coverage UNVERIFIED. | <https://www.alphavantage.co/premium/> |
| **Marketstack** | 70 exchanges; **Indonesia/XIDX not confirmed on the vendor page — UNVERIFIED**. History tiered: free 1 yr, Basic $9.99 10 yr, Pro $49.99 15+ yr. Free tier is 100 requests/month. | <https://marketstack.com/product> |
| **Stooq** (free CSV) | **No longer machine-accessible.** `https://stooq.com/q/d/l/?s=bbca.jk&i=d` and the `spy.us` equivalent both returned a JavaScript browser-verification challenge instead of CSV. Not automatable. | probe |
| **IDX itself** (idx.co.id) | Their own site sits behind Cloudflare; `https://www.idx.co.id/en/market-data/data-services/` returned **HTTP 403** to a normal fetch and the internal `primary/TradingSummary/GetStockSummary` endpoint returned a Cloudflare challenge page. **No documented public API.** Official IDX data-services terms could not be read — **UNVERIFIED**. | probe |
| **OHLC.dev / Invezgo / iTick** | All claim IDX daily OHLCV. OHLC.dev is distributed via RapidAPI, mentions LQ45 constituents, and carries the disclaimer "Market data may not always be real-time or fully accurate"; it states no IDX partnership. None documents split/dividend adjustment or delisted coverage. Small-vendor continuity risk. **Adjustment policy and history depth UNVERIFIED for all three.** | <https://ohlc.dev/indonesia-stock-exchange-idx-api> |

---

## 3. Split/dividend adjustment — the finding that matters most

**The requirement:** trades store the price actually paid (unadjusted, from the broker statement); indicators (MA10/20/50/100/200, ADR, ATR, prior-1M move) need one consistent adjusted series.

**The finding: Yahoo does not expose a truly unadjusted OHLC series.** What it calls unadjusted is *split-adjusted but not dividend-adjusted*. Proven by probe (2026-08-12, `auto_adjust=False`):

AAPL across its 4:1 split on 2020-08-31 —

| Date | Open | Close | Adj Close | Volume | Stock Splits |
|---|---|---|---|---|---|
| 2020-08-27 | 127.14 | 125.01 | 121.15 | 155,552,400 | 0 |
| 2020-08-28 | 126.01 | 124.81 | 120.96 | 187,630,000 | 0 |
| 2020-08-31 | 127.58 | 129.04 | 125.06 | 225,702,700 | **4** |
| 2020-09-01 | 132.76 | 134.18 | 130.04 | 151,948,100 | 0 |

AAPL actually traded around $500 on 2020-08-28. The pre-split rows are shown at ~$126, i.e. **already divided by 4**.

BBCA.JK across its 1:5 split on 2021-10-13 —

| Date | Open | Close | Adj Close | Volume | Stock Splits |
|---|---|---|---|---|---|
| 2021-10-11 | 7,240 | 7,255 | 6,216.38 | 47,201,000 | 0 |
| 2021-10-12 | 7,255 | 7,320 | 6,272.07 | 91,067,000 | 0 |
| 2021-10-13 | 7,400 | 7,525 | 6,447.72 | 210,893,300 | **5** |
| 2021-10-14 | 7,600 | 7,750 | 6,640.51 | 138,811,900 | 0 |

BBCA traded around IDR 36,000 in early October 2021. Same story.

Note also that `Adj Close` ≠ `Close` even on rows far from any split (BBCA 2021-10-11: 7,255 vs 6,216.38) — that gap is the accumulated dividend adjustment. On the most recent bars the two converge exactly (BBCA 2026-08-11: Close 6,300.0, Adj Close 6,300.0), which is the expected behaviour of a back-adjusted series anchored at the present.

**Where each source lands:**

| Source | Truly raw OHLC | Split-adjusted OHLC | Split+div adjusted | Split/div event series |
|---|---|---|---|---|
| Yahoo / yfinance | **No** | Yes (`auto_adjust=False` OHLC) | Yes (`Adj Close`, or `auto_adjust=True`) | **Yes** (`Dividends`, `Stock Splits` columns) |
| EODHD | **Yes** (`open/high/low/close`) | Yes (Technical API `function=splitadjusted`) | Yes (`adjusted_close`) | Yes |
| IBKR | **No** — `TRADES` "is adjusted for splits, but not dividends" | Yes (`TRADES`) | Yes (`ADJUSTED_LAST`, TWS 967+) | via separate corporate-action requests |

Sources: <https://eodhd.com/financial-apis/api-for-historical-data-and-volumes>, <https://interactivebrokers.github.io/tws-api/historical_bars.html>.

**Design consequence.** With yfinance (or IBKR) you get *two* series but neither is raw. Two implications:

1. **Indicators are fine.** Use one consistent series. Recommendation: **split-adjusted, dividend-unadjusted OHLC** (`auto_adjust=False`) for MAs/ADR/ATR — it is the series a chart-reading momentum trader is actually looking at, and it keeps High/Low honest for ATR. Do **not** mix `Adj Close` into an OHLC-based indicator; the dividend adjustment scales Close but not the H/L in the same frame unless you use `auto_adjust=True` throughout.
2. **Backdated trades across a split will not reconcile.** A manually entered BBCA trade at IDR 36,000 in Sept 2021 will sit against bars showing ~7,200. The journal **must store the entry/exit price as typed** and reconcile to bars through a **cumulative split factor** computed from the `Stock Splits` series, which yfinance does provide. **This is a spec requirement, not an implementation detail — it belongs in #7/#9.** It also means a *later* split silently invalidates the price-vs-bar relationship of already-enriched historical trades — which is exactly the **restatement** problem the map lists as unspecified.

---

## 4. Delisted and renamed tickers

**Yahoo/yfinance: delisted history is NOT retrievable, and renamed symbols are actively dangerous.** Probe (2026-08-12):

| Symbol | Result |
|---|---|
| `TWTR` (delisted Oct 2022) | **Empty.** Warning: "possibly delisted; no timezone found" |
| `SIAP.JK` (delisted IDX name) | **Empty**, HTTP 404 "Quote not found for symbol: SIAP.JK" |
| `META` (renamed from FB, Jun 2022) | 3,577 rows, **2012-05-18 → 2026-08-11** — full pre-rename history (back to the 2012 IPO) carried forward under the new symbol ✅ |
| `FB` | **283 rows, 2025-06-26 → 2026-08-11** — a *different instrument* now occupies the ticker |
| `TRIO.JK` | **1 row**, 2026-07-17 — junk/reused |

Two distinct failure modes, and the second is worse:

- **Delisting ⇒ silent data loss.** The series simply disappears. A closed trade in a since-delisted name loses its enrichment on any re-run.
- **Ticker reuse ⇒ silent data *corruption*.** `FB` returns 283 rows of a completely unrelated instrument. Nothing in the response says "this is not the Facebook you meant." An enrichment pipeline keyed on ticker string alone will happily compute an MA200 from the wrong company's bars.

**Mitigations the spec must adopt regardless of source:**
1. **Snapshot enrichment at compute time and never silently recompute.** Once a trade's bars are fetched and its fields derived, persist them. Re-derivation must be an explicit, logged, diffable operation.
2. **Store the fetch date and source alongside every enriched field**, so a later restatement can tell what was computed from what.
3. **Sanity-gate every fetch:** if the returned series does not span the trade's own date range, treat it as a failure, not as "no data". `FB` would fail this gate cleanly.

**EODHD** is materially better here: `delisted=1` retrieves delisted tickers (<https://eodhd.com/financial-apis/list-supported-exchanges>). Whether *price history* for a delisted symbol is served, and how far back, is **UNVERIFIED** — the ticker-list endpoint documentation does not say.

**Practical note:** for a swing trader holding for days-to-weeks, delisting mid-trade is rare and manual repair of a handful of trades is acceptable. Ticker *reuse* is the one that needs an automated guard.

---

## 5. Is IBKR's own historical data API viable?

**For the US: yes, technically — but ranked #3, not #1.**

- **Endpoint.** TWS API `reqHistoricalData`, or the Web API `/iserver/marketdata/history` (the older `/hmds/history` has been deprecated and removed from the docs). Sources: <https://interactivebrokers.github.io/tws-api/historical_bars.html>, <https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-changelog/>.
- **Adjustment.** Both series available: `TRADES` "is adjusted for splits, but not dividends"; `ADJUSTED_LAST` "is adjusted for splits and dividends. Requires TWS 967+" (<https://interactivebrokers.github.io/tws-api/historical_bars.html>). Same shape as Yahoo — **no truly raw OHLC**.
- **Rate limits — restrictive.** From <https://interactivebrokers.github.io/tws-api/historical_limitations.html>: no identical requests within 15 seconds; no 6+ requests for the same contract/exchange/tick-type within 2 seconds; **"Making more than 60 requests within any ten minute period"** triggers a pacing violation; max 50 simultaneous open historical requests. The Web API `/iserver/marketdata/history` endpoint is limited to 5 concurrent requests. **60 req / 10 min is 6 per minute** — workable for a small portfolio's daily job, painful for a bulk backfill of backdated trades.
- **Cost.** Historical data requires a market data subscription; the **US Securities Snapshot and Futures Value Bundle is USD 10.00/month for non-professional subscribers** (USD 30.00 professional) — <https://www.interactivebrokers.com/en/pricing/research-news-marketdata.php> (2026-08-12).
- **The real blocker: session model.** Both APIs require an authenticated, running gateway (TWS or IB Gateway / Client Portal Gateway) with periodic re-authentication, not a stateless bearer token. Against the map's "daily background job on a host that outlives any UI session", this is the single largest operational cost of the IBKR route. This is a well-known property of the IBKR API architecture; the specific re-auth interval is **UNVERIFIED**.
- **Daily-bar history depth: UNVERIFIED.** The limitations page documents that sub-30-second bars older than six months are unavailable but "does not specify how far back daily bars extend".

**For IDX: no. Categorically.** IBKR's own Asia-Pacific stock commissions page lists exactly seven markets — **Australia, Hong Kong, India, Japan, Malaysia, Singapore, Taiwan**. Indonesia does not appear (<https://www.interactivebrokers.com/en/pricing/commissions-stocks-asia-pacific.php>, fetched 2026-08-12). The string "Indonesia" appears **zero times** in IBKR's market data pricing page. IBKR cannot serve the IDX half of this journal.

> **Access note:** `interactivebrokers.com` and `interactivebrokers.co.uk` were unreachable from this network via normal HTTPS — TLS presented a certificate for `*.ioh.co.id` (Indosat), i.e. ISP-level DNS interception. The pages above were retrieved by pinning the Akamai IP resolved via `1.1.1.1`. Worth knowing: **if the daily job runs on an Indonesian residential connection, IBKR API reachability is itself a risk.**

**Verdict:** even if IBKR were perfect for the US, it would force a second source for IDX. Since a second source is needed anyway, and that source (yfinance or EODHD) already covers the US, IBKR's marginal value is *authoritative agreement with the broker's own fills* — real, but not worth $10/mo plus a gateway daemon plus a 6-req/min ceiling. **Keep it as a reconciliation spot-check, not a pipeline.**

---

## 6. Index / benchmark series for regime classification

All four required series are available free via yfinance (probe, 2026-08-12):

| Market | Instrument | Symbol | History from | Rows |
|---|---|---|---|---|
| US | SPY | `SPY` | 1993-01-29 | 8,440 |
| US | QQQ | `QQQ` | 1999-03-10 | 6,898 |
| US | S&P 500 index | `^GSPC` | **1927-12-30** | 24,769 |
| US | Nasdaq-100 index | `^NDX` | 1985-10-01 | 10,294 |
| US | Russell 2000 ETF | `IWM` | 2000-05-26 | 6,590 |
| IDX | **IHSG** (Jakarta Composite) | `^JKSE` | **1990-04-06** | 8,847 |
| IDX | **LQ45** | `^JKLQ45` | **1997-02-24** | 7,155 |

This is the strongest single argument for yfinance. **IHSG and LQ45 with 29- and 36-year histories, free, from the same client as the stock bars.** No other candidate was confirmed to carry `^JKLQ45` at all — EODHD's index coverage for Indonesia is **UNVERIFIED**, and it is the one gap that would hurt on a switch.

Note the ETF-vs-index choice: `SPY`/`QQQ` carry dividends and tracking error; `^GSPC`/`^NDX` are price indices with deeper history and no distributions. For **regime classification** (is the market above its MA200, etc.) the price indices are cleaner and longer. Recommend `^GSPC`/`^NDX` for US regime and `^JKSE`/`^JKLQ45` for IDX regime, keeping SPY/QQQ only if a tradeable-benchmark comparison is ever wanted.

---

## 7. Ranked recommendation and switch triggers

### Primary: **yfinance for both markets.**

Adopt with three non-negotiable engineering conditions:

1. **A local bar cache is part of the design, not an optimisation.** Bars, once fetched, are stored. The pipeline reads the cache; the daily job fills it. This makes the journal survive a Yahoo outage, block, or breaking change, and it makes enrichment reproducible.
2. **A source-adapter seam.** Everything above the fetch layer speaks a market-neutral bar interface. Swapping to EODHD must be one adapter, not a refactor.
3. **A ticker-identity guard** on every fetch (§4): reject any series that does not span the trade's own dates.

### What would make me switch to EODHD ($19.99/mo)

Any one of these:

- **Yahoo blocks or degrades the endpoint** — sustained 429s that backoff does not clear, or the cookie/crumb flow breaks and yfinance does not ship a fix within a couple of weeks.
- **The terms question stops being theoretical** — the user decides the ToS automated-access clause (§2.1) is not an acceptable risk to run knowingly. This is a judgement call for the human, not for me; it is entirely reasonable to pay $19.99/mo to make it go away.
- **A genuinely unadjusted OHLC series turns out to be needed** rather than reconstructible from split factors — i.e. the split-factor reconciliation in §3 proves too fiddly in practice.
- **Deep IDX backdating matters** — trades before ~2002 for a `.JK` name Yahoo starts late on. EODHD claims non-US coverage "mostly from Jan 3, 2000".
- **Delisted-name history becomes a recurring problem** rather than a once-a-year manual repair.

**Before switching, verify on EODHD's free tier:** (a) actual JK history start dates for a few names, (b) whether `^JKSE`/LQ45 equivalents exist, (c) whether delisted `.JK` *price history* (not just the ticker listing) is served. All three are currently UNVERIFIED and all three are things yfinance demonstrably does or does not do.

### What would make me switch to IBKR for the US

Essentially only: **the journal starts disagreeing with the broker on fills or corporate actions in a way that matters.** Then use IBKR as a *reconciliation source* against the yfinance series, not as a replacement. A full switch would also require accepting the gateway daemon, and would still leave IDX unsolved.

### What would *not* make me switch

Marketstack, Alpha Vantage, Massive and Tiingo were all screened out on structural grounds (US-only, tiered history, or unusable free limits) — none of them becomes attractive at a different price. The niche IDX vendors would only come into play if **both** yfinance and EODHD failed on IDX, and each would need its adjustment policy established from scratch first.

---

## 8. Open questions and explicitly unverified claims

| Claim | Status | Who should close it |
|---|---|---|
| EODHD JK exchange history start date | **UNVERIFIED** — vendor gives only "non-US mostly from Jan 3, 2000" globally | free-tier probe before any switch |
| EODHD Indonesian index series (`^JKSE`/LQ45 equivalents) | **UNVERIFIED** — not found on vendor pages | free-tier probe |
| EODHD price history for *delisted* tickers (vs just listing them) | **UNVERIFIED** | free-tier probe |
| Twelve Data history depth per exchange | **UNVERIFIED** | n/a unless Twelve Data is reconsidered |
| Marketstack Indonesia/XIDX coverage | **UNVERIFIED** | n/a — screened out on tiered history anyway |
| IBKR daily-bar maximum history depth | **UNVERIFIED** — docs cover only sub-30s bars | n/a unless IBKR is promoted |
| IBKR gateway re-authentication interval | **UNVERIFIED** | n/a unless IBKR is promoted |
| Stockbit private/partner data API | **UNVERIFIED** — no public developer portal found | ask Stockbit directly, if it ever matters |
| Official IDX data-services terms and pricing | **UNVERIFIED** — idx.co.id returned HTTP 403 / Cloudflare challenge | n/a |
| OHLC.dev / Invezgo / iTick adjustment policy, history depth, delisted coverage | **UNVERIFIED** for all three | only if both #1 and #2 fail |
| Whether EODHD's "delete all copies within one month" clause reaches *derived* enrichment fields, not just cached bars | **UNVERIFIED** — terms do not address derived data | relevant to #7/#9 and to the restatement question in #1 |

### Things this ticket surfaced that belong to other tickets

- **Split-factor reconciliation between stored trade prices and adjusted bars** (§3) is a data-model requirement — feeds **#7 / #9**.
- **Enrichment snapshotting and the ticker-identity guard** (§4) are capture/enrichment-flow requirements — feeds **#7 / #9**.
- **A later split silently invalidates already-enriched historical trades** — this is a concrete instance of the map's open "restatement" question, and now has a named trigger.
- **IDX zero-volume days** appear in the data (4 in BBCA.JK 2026 YTD). Suspensions/halts and auto-reject limits are already flagged in the map as unspecified; the bar series will show them as zero-volume rows rather than gaps, which the indicator code must handle.

---

## Appendix: reproducing the probes

Run against yfinance 1.5.2 on 2026-08-12.

```python
import yfinance as yf

# coverage + history depth
for s in ["AAPL","SPY","QQQ","^GSPC","^NDX","IWM",
          "BBCA.JK","BBRI.JK","TLKM.JK","ANTM.JK","GOTO.JK","BREN.JK",
          "PTBA.JK","MDKA.JK","^JKSE","^JKLQ45",
          "FB","META","TWTR","SIAP.JK","TRIO.JK"]:
    h = yf.Ticker(s).history(period="max", interval="1d",
                             auto_adjust=False, actions=True)
    print(s, len(h), h.index.min(), h.index.max())

# adjustment semantics
print(yf.Ticker("AAPL").history(start="2020-08-27", end="2020-09-02",
                                auto_adjust=False, actions=True))
print(yf.Ticker("BBCA.JK").history(start="2021-10-11", end="2021-10-15",
                                   auto_adjust=False, actions=True))
```

Raw-endpoint rate-limit check (expected to fail with 429, demonstrating that yfinance's session handling is required):

```
curl -A "Mozilla/5.0" \
  "https://query1.finance.yahoo.com/v8/finance/chart/BBCA.JK?range=max&interval=1d"
```
