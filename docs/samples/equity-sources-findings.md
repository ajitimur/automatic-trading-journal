# Verifying what the equity sources actually return

Empirical answers to
[#19](https://github.com/ajitimur/automatic-trading-journal/issues/19), which exists because
[#15](https://github.com/ajitimur/automatic-trading-journal/issues/15) reached a per-book
verdict on account equity **by inference from documentation** — and
[#2](https://github.com/ajitimur/automatic-trading-journal/issues/2) had already shown what
that costs, when IBKR's documented "commission on the first partial execution only" turned out
to undercount real multi-fill orders by ~60%.

Date: 2026-08-13. Sources: the same July 2026 Stockbit Statement of Account already in hand
from #2, and live calls against the IBKR Flex Web Service.

**No balances, account numbers or names appear in this file or its siblings.** The raw
documents stay in the vault outside the repo. Layout and arithmetic identities are real;
every figure is scrubbed.

---

## The headline

**The IDX verdict was too pessimistic, and by more than the ticket hoped for.** #19 asked
whether the SoA's later pages carry a holdings table, and whether it is valued or
quantities-only — the compliant-and-useless case POJK 13/2025 permits. It carries a valued
table **and, on page 1, a single printed account-equity figure** (`Equity NAB`). Nothing has
to be reconstructed from cash plus a Fill ledger, which is the unbounded-level-shift trap #15
correctly refused.

**But it hands over two different candidate denominators, not one**, and the document does not
say which is "account equity". That is a decision, and it belongs to
[#16](https://github.com/ajitimur/automatic-trading-journal/issues/16).

**The IBKR side is half-settled.** The two traps #15 warned about were both confirmed AFK
against the live service, and one new one surfaced. The daily-series question — the single
point everything on the IBKR side rests on — still needs the account owner in the portal;
`scripts/equity-nav-wizard.sh` walks that.

---

## Stockbit — answered, and the verdict upgrades

Structure, layout hazards and the verified identities are in
[`stockbit-soa-equity-structure.txt`](stockbit-soa-equity-structure.txt). The findings:

| # | #19 asked | Answer |
|---|---|---|
| 1 | Is there a securities-position table at all? | **Yes** — `PORTFOLIO STATEMENT`, on the last page, after the ledger. |
| 2 | Market values and a total, or quantities only? | **Fully valued.** Per row: `Quantity`, `Buying` price, `Close` price, `Buying Value`, `Market Value`, unrealised gain/loss in Rp and %. Plus a printed `TOTAL`. |
| 3 | Does the IDX verdict change? | **Yes — it upgrades**, and further than "valued holdings at month-end". |

**It is self-checking, three ways over.** `Market Value = Quantity × Close` holds exactly on
every row; the rows sum exactly to the printed `TOTAL`; and that `TOTAL` equals the header
box's `Portfolio`. This is the same property the daily Trade Confirmation's fee model has —
a parser can assert its own correctness rather than trust its column positions, and layout
drift breaks the identity immediately instead of silently.

### The catch: two printed candidates for the denominator

The page-1 summary box carries both `Equity NAB` and the ingredients of a different total. The
identities, verified to the rupiah:

```
Equity NAB = Portfolio + <ledger closing balance>          (exact)
Cash       = Cash Investor + <ledger closing balance>      (within Rp 1; see the structure file)
```

So **`Equity NAB` includes the unsettled trading balance but excludes `Cash Investor`** — and
on this sample `Cash Investor` is not a rounding detail: `Equity NAB` and `Portfolio + Cash`
differ by roughly **a fifth of the larger figure**.

Risk % is `(entry − stop) × size ÷ equity`. A denominator ~20% off moves every risk % on the
IDX book by ~20%, straight through the 1% flag #1 settled. This is exactly the direction of
error #15 was alert to — and note it cuts the *flattering* way if the larger figure is the
honest one: a too-small denominator **overstates** risk %, which is the safe direction, while
picking the larger one when the cash is not really trading capital understates it.

**What `Cash Investor` actually is, is unresolved.** The statement is co-branded
`PT. STOCKBIT SEKURITAS DIGITAL` / `PT. BIBIT TUMBUH BERSAMA` in the letterhead — Bibit being
the mutual-fund sibling on the same RDN. A plausible reading is that `Cash Investor` is the
RDN bank balance, shared with the mutual-fund business and therefore not brokerage trading
capital, which would explain why the broker's own equity figure excludes it. **That is a
hypothesis, not a finding** — it rests on one month's document. Two ways to settle it, both
cheap and neither done here:

- a second month's SoA, checking whether the identities and the gap hold; and
- comparing `Equity NAB` against the daily equity series in Stockbit's **Portfolio
  Performance** UI (#15 established it runs back to Jan 2024) for the same date. If Portfolio
  Performance agrees with `Equity NAB`, the question is closed — the app the trader actually
  looks at defines the number.

The second is the stronger test and it costs one screenshot.

### Granularity, and what it does not solve

**One snapshot per month, at month end.** That is 12 anchor points a year, against a July 2026
floor. It does not by itself supply equity on an arbitrary entry date, so it does **not**
retire hand entry — #15's finding that Portfolio Performance is UI-only still stands.

What it changes is the *character* of the hand entry: there is now an authoritative monthly
figure to **reconcile hand-entered snapshots against**, and it is machine-readable from a
document already in the intake path. Whether that makes month-end anchors plus interpolation
good enough, or whether hand entry stays primary with the SoA as a check, is #16's to decide.

### Two things found in passing

- **Dividend cash is in the SoA ledger.** Matched accrual/payment rows carrying share count,
  per-share rate and the withholding rate. This corrects a premise carried in
  [#17](https://github.com/ajitimur/automatic-trading-journal/issues/17), which stated IDX
  dividend cash is "not in the intake path at all". More precisely: it is absent from the
  *daily* Trade Confirmation, and present in the *monthly* SoA. **#17's decision is not
  reopened by this** — it declined to attribute dividend cash because a dividend accrues to a
  `Position` and would drag in #6's FIFO machinery, and because realised R stays a pure price
  measure. Those legs are untouched. Only the availability claim was too strong.
- **The SoA is lossy for fills, not for cash.** #2 rejected the SoA as an intake path because
  it collapses fills to a weighted average. That verdict stands for *trades* — and is exactly
  why the equity sections are worth having: they are the parts of the document that lose
  nothing.

---

## IBKR — the traps confirmed, the series still unverified

### Confirmed: a Flex error is HTTP 200 with an error body

Sent a deliberately invalid token to `SendRequest`:

```
HTTP_STATUS=200
CONTENT_TYPE=text/xml;charset=UTF-8

<FlexStatementResponse timestamp='...'>
<Status>Fail</Status>
<ErrorCode>1015</ErrorCode>
<ErrorMessage>Token is invalid.</ErrorMessage>
</FlexStatementResponse>
```

#15 inferred this; it is now observed. **A client that checks the HTTP status is blind to
every Flex failure.** For an unattended daily job this is the difference between "the equity
series is empty today" and "the token died three weeks ago" — the job must parse `<Status>`
and treat an empty series as an error, never as a value.

### Confirmed: the existing token is alive, and was not disturbed

`SendRequest` against the existing Executions query returned `<Status>Success</Status>`. The
token from #2 (expiring 2027-07-14) is valid and **nothing in this work regenerated it** —
generating a token silently invalidates the current one, which would kill the trades job.
The wizard reuses the vault token and refuses to run rather than mint a new one.

### New: `SendRequest` and `GetStatement` are on *different hosts*

`SendRequest` is served from `ndcdyn.interactivebrokers.com`, but its success response names
the follow-up endpoint explicitly:

```xml
<Url>https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>
```

A different host. #2's wizard hardcoded `ndcdyn` for both calls and happened to work, so this
went unnoticed. **Both hosts are DNS-intercepted on this ISP**, so it matters:
[#18](https://github.com/ajitimur/automatic-trading-journal/issues/18) settled that DNS is
resolved over DoH inside the Flex client, per run, never cached — this sharpens it to **per
host, driven by the response**, not one constant resolved once. A client that resolves only
`ndcdyn` over DoH fails on the second call, and fails as an empty body.

### New: the ISP's block address has changed

| | address returned for the Flex hosts |
|---|---|
| #2 (2026-08-12) | `114.7.173.245` / `.246` |
| #19 (2026-08-13) | `202.169.44.80` |

Same interception, different block address — and the same address for both Flex hosts, while
the real Akamai edges differ per host. **Detect interception by mismatch against the DoH
answer, never by matching a known block address.** A hardcoded blocklist would have read
today's interception as a clean resolve. (The DoH answers rotate too, as #2 already found.)

### Still unverified — needs the account owner in the portal

Everything on the IBKR side rests on one unverified point, and it cannot be settled without
creating a second Flex Query, which only the account owner can do:

- **Does the NAV section emit one `<EquitySummaryByReportDateInBase>` row per `reportDate`, or
  only period start/end?** IBKR's prose describes the PDF layout and reads as denying the
  series. If it is endpoints-only, #15's IBKR verdict degrades sharply and #16's shape changes.
- **Are there gaps**, and what is the **earliest `reportDate`** actually returned — does
  `N > 365` work, or is it clamped? (#2 found the period control offers presets only, deepest
  `Last365CalendarDays`. Whether the NAV section behaves the same is unknown; #15 found Flex is
  retrospective four calendar years plus YTD, which the presets did not reflect.)
- **Does `total` behave like net liquidation value** — is `total ≈ cash + stock`?
  (`netLiquidation` does not exist in Flex; the equivalence is inferred.)

**Run `scripts/equity-nav-wizard.sh`.** It walks the portal steps, fetches over the DoH path
with per-host resolution, probes the XML for all four questions, re-confirms the error shape
against the new query id, and writes redacted answers to `ibkr-nav-flex-findings.md`. Its
probe was tested against synthetic NAV files for both the daily-series and the
wrong-section-selected cases.

---

## Bottom line for [#16](https://github.com/ajitimur/automatic-trading-journal/issues/16)

- **IDX is no longer hand-entry-only.** A valued, self-checking, month-end equity figure is
  machine-readable from a document already in the intake path. Hand entry is not retired —
  monthly granularity does not cover arbitrary entry dates — but it now has an authoritative
  reconciliation anchor, which changes what #16 is choosing between.
- **#16 inherits a live question the document cannot answer**: `Equity NAB` or
  `Portfolio + Cash`? They differ by ~20%, and risk % scales inversely with the choice. The
  Portfolio Performance comparison settles it for the cost of one screenshot.
- **The IBKR side keeps its shape but not yet its confidence.** Both silent killers are now
  observed rather than inferred, and a third (two hosts) was found. The daily-series
  cardinality is still the load-bearing unknown, and #16 should not assume it.
