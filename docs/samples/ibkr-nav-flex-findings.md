# IBKR NAV Flex — verification findings

The IBKR half of [#19](https://github.com/ajitimur/automatic-trading-journal/issues/19),
answered against a real statement. Query built in the portal by the account owner on
2026-08-13; fetched and probed the same day.

A **second** Activity Flex Query, `Net Asset Value (NAV) Summary in Base` only, separate from
#2's Executions query. The existing Flex token was reused — **no token was generated**, since
generating one silently invalidates the live token the trades job depends on.

**No balances appear in this file.** The raw statement is a year of real account equity and
stays in the vault, gitignored. Counts, dates and ratios only.

---

## The headline

**#15's inference was right, and IBKR's own prose is what was misleading.** The NAV section
emits a genuine daily series — **262 rows, one per `reportDate`, zero duplicates, zero gaps.**
The documentation describes the PDF layout, which reads as though only period endpoints exist.
It does not.

Two things turned up that neither #15 nor #19 anticipated, and both change what
[#16](https://github.com/ajitimur/automatic-trading-journal/issues/16) is designing:

1. the series is **weekday-dense, not trading-day-dense**; and
2. the reachable window is a **rolling 365 days**, so early history ages out.

---

## The four questions

### 1. Daily series, or only period start/end? — **daily, confirmed**

```
<EquitySummaryByReportDateInBase> rows: 262
distinct reportDate values:            262
duplicated dates:                        0
```

One row per report date, no aggregation, no endpoints-only degenerate case. The element is
`EquitySummaryByReportDateInBase`, nested in a single `EquitySummaryInBase` — and note again
that "Equity Summary in Base" names no *section* in the portal, exactly the false negative #15
flagged. The section to tick is **Net Asset Value (NAV) Summary in Base**.

Attributes available on each row:

```
accountId  reportDate
cash  cashLong  cashShort
stock stockLong stockShort
total totalLong totalShort
```

### 2. Gaps, and how far back? — **no gaps; 366 days; clamped at the 365-day preset**

```
span      2025-08-11 .. 2026-08-11  =  366 calendar days
weekdays in span                    =  262
rows present                        =  262   (100.0%)
weekdays with no row                =    0
```

**Zero missing weekdays.** But the period control offered only the presets #2 already
documented — `Last365CalendarDays` is the deepest — so **`N > 365` was not available**. #19
asked whether a larger N works; the answer is that the form never offers it. #15's finding that
Flex is retrospective across four calendar years plus YTD is not contradicted, but it is **not
reachable through this control**; whether a year-based preset reaches further is untested.

**This is the consequential one.** The window is *rolling*: it reaches back one year from
whenever it runs. It clears the **July 2026 floor comfortably today** — but around **July 2027
the floor falls out of the window**, and a Trade entered in July 2026 could no longer have its
equity re-derived.

> **Consequence for #16: equity snapshots must be captured and persisted, never re-derived on
> demand.** This is not an optimisation, it is the only way the July 2026 floor survives its
> first birthday. It sits comfortably with #12's local bar cache and #18's "raw source
> documents kept forever" — the NAV XML is a raw source document and belongs in that tier.

### 3. Does `total` behave like net liquidation value? — **yes, and it is the right field**

```
total == cash + stock exactly (<1 cent):  223 / 262
rows carrying a residual:                  39
worst residual as a share of total:      0.0061%   (on 2026-07-02)
rows where total <= 0:                      0
rows where total != totalLong + totalShort: 0
```

`total` is **not definitionally `cash + stock`** — 39 rows carry a small residual, presumably
accruals (dividend/interest) that sit outside the two headline buckets. But the residual peaks
at **0.0061% of total**, which is nothing against a risk-% denominator.

The residual is a reason to use `total`, not a reason to distrust it: it is the *more complete*
figure, and reconstructing `cash + stock` by hand would silently drop it. **Use `total`.**
`netLiquidation` does not exist in Flex — that is TWS vocabulary, as #15 correctly warned.

**`cash` was never negative in this sample** (0 of 262). #15 warned that `cash` goes negative on
margin and is therefore the wrong denominator. That hazard is **unconfirmed, not refuted** —
this account simply did not use margin over the window. The guidance stands on its own logic.

### 4. Error shape — **HTTP 200 with an XML error body, confirmed twice**

Already settled AFK before the query existed, and it holds. A deliberately invalid token:

```
HTTP_STATUS=200
CONTENT_TYPE=text/xml;charset=UTF-8

<FlexStatementResponse timestamp='...'>
<Status>Fail</Status>
<ErrorCode>1015</ErrorCode>
<ErrorMessage>Token is invalid.</ErrorMessage>
</FlexStatementResponse>
```

**A client that checks the HTTP status is blind to every Flex failure.** Parse `<Status>`, and
treat an empty series as an error rather than a value — this is the trap that would let an
unattended job record "no equity" instead of failing loudly.

---

## Two findings the ticket did not ask for

### The series is weekday-dense, not trading-day-dense

There is a NAV row on **every US market holiday** in the window — Labor Day, Thanksgiving,
Christmas, New Year, MLK, Presidents, Good Friday, Memorial, Juneteenth, and the observed
July 4th. All ten present. The account's NAV is simply carried forward on days the market never
opened.

This collides directly with
[#17](https://github.com/ajitimur/automatic-trading-journal/issues/17)'s trading-day invariant —
*a zero-volume row is not a trading day*, filtered at the bar-cache boundary. **The equity
series has rows for days the bar cache deliberately does not.** Joining equity to bars on date
must therefore tolerate equity rows with no matching bar; it must never infer a missing trading
day from a present equity row, nor treat the surplus rows as data corruption.

It cuts the useful way for risk %, though: a Trade's entry date is always a trading day, so an
equity row is always there for it. The hazard is in the other direction — counting rows to
measure elapsed trading days would overcount.

### Equity lags by at least a day

The query ran late on 2026-08-12 EDT and the freshest row is **2026-08-11**. Statements settle
overnight, so the most recent equity available is T-1 or T-2, never T.

**Consequence for #16 and #18**: a Trade confirmed on its entry day cannot have an entry-dated
risk % computed that same day — the denominator does not exist yet. This is not a problem so
much as a scheduling fact, and #10 already built the machinery for it: enrichment runs on two
clocks, and a missing input **holds enrichment without blocking the commit**. Risk % simply
fills in on the next daily run. Worth stating explicitly so nobody designs a same-day risk-%
display that can never work.

---

## Transport notes, confirmed on this run

- `SendRequest` is served from `ndcdyn.interactivebrokers.com`; its response names
  `gdcdyn.interactivebrokers.com` for `GetStatement`. **Two hosts, both DNS-intercepted here.**
  Resolve **per host, driven by the response** — #2's wizard hardcoded one host and happened to
  work.
- Both hosts resolved to the same ISP block address under system DNS while their true Akamai
  edges differ. On this run `ndcdyn` and `gdcdyn` happened to return the *same* Akamai address,
  where an earlier lookup gave different ones — so **cache nothing and hardcode nothing**, in
  either direction.
- The DoH path returned `Status Success` and a well-formed statement on the first attempt.
