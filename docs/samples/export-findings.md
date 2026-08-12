# Sample export findings

Empirical answers to the open items left UNVERIFIED by
[#4](https://github.com/ajitimur/automatic-trading-journal/issues/4) and
[#5](https://github.com/ajitimur/automatic-trading-journal/issues/5).
Resolves [#2](https://github.com/ajitimur/automatic-trading-journal/issues/2) for the
Stockbit book; the IBKR book is still outstanding (see [IBKR](#ibkr--still-outstanding)).

Date: 2026-08-12. Samples: one daily Trade Confirmation (trade date 11/08/2026) and one
monthly Statement of Account (July 2026), both from the account owner's mailbox.

**Raw PDFs are deliberately not committed** — they carry name, address, NPWP/NIK, phone,
account number and RDN bank details. Redacted structure extracts are in
`stockbit-tc-structure.txt` and `stockbit-soa-structure.txt`.

---

## The headline

**The Trade Confirmation preserves individual fills. The Statement of Account does not.**

The TC lists one row per fill, with multiple fills of the same order sharing a `REF #`.
The SoA collapses those same fills into a single row at a weighted-average price — which is
why SoA prices carry up to four decimals (`MARK 96,800 @ 977.2882`) that are not valid IDX
ticks.

This was suspected in #5; it is now demonstrated. The TC is the intake path and the SoA is a
month-end reconciliation check, exactly as #5 proposed — but the reason is stronger than
"the TC is more detailed": **the SoA is lossy in a way that cannot be inverted.**

---

## Stockbit — must-confirm list, answered

| # | Question | Answer |
|---|---|---|
| 1 | Clean text layer from `pdftotext -layout`? | **Yes.** TC 5,369 chars / 1 page; SoA 22,771 chars / 5 pages. Producer `FPDF 1.7` — generated, never rasterised. **No OCR needed.** |
| 2 | Password-protected? | **No.** Neither document. Nothing to store beside the job. |
| 3 | Exact column headers | **`REF #  Board  Share  Lot  Quantity  Price  Buy  Sell`** (verbatim). |
| 4 | Quantity in lots or shares? | **Both, in separate columns.** `Lot` 619 and `Quantity` 61,900.00 on the same row. `Quantity = Lot × 100`. No inference, no 100× guessing — read `Quantity` and store shares. |
| 5 | Partial fills: rows or daily average? | **Separate rows, one per fill.** Three FUTR rows share `REF # 0477722` at 21,800@346 / 28,200@348 / 49,200@358. `REF #` is the **order**; the row is the **fill**. |
| 6 | Fees itemised or one total? | **Fully itemised** — Commission, V.A.T Commission, IDX Fee, V.A.T Levy, Income Tax, Stamp Duty — but **per side for the whole day, not per row.** |
| 7 | Settlement date distinct from trade date? | **Yes.** `Transaction Date 11/08/2026` / `Settlement Date 13/08/2026` — T+2, both on the header. |
| 8 | Execution time shown? | **No.** No time column anywhere. Same-day fills can only be ordered by `REF #` and row order. |
| 9 | Does it paginate? | **Unknown.** 9 rows fitted one page. Still open — needs a heavier day. |
| 10 | Zero-activity day → empty TC? | **Still open.** Needs observation over time, not one sample. |
| 11 | Sekuritas T&C on automated processing | **Still open.** Not answerable from a statement. |

## The fee model — exact, and self-checking

Every figure below reconciles to the rupiah against the sample:

```
buy  cost = gross + 0.15% + Rp10,000 stamp duty
sell cost = gross − 0.15% − 0.10% income tax
```

- The document's own `Total Fee : 0.1500 % All In` **excludes stamp duty and income tax**.
  The 0.15% bundle decomposes into Commission + V.A.T Commission + IDX Fee + V.A.T Levy, and
  sums to exactly 0.150000% on both sides.
- **Stamp duty is buy-side only** and **flat Rp10,000 per document** — not per trade, not
  proportional. A day with six buys still pays Rp10,000 once.
- **Income tax is sell-side only**, exactly 0.100% of sale proceeds.
- `Total Cost` and `Payment due to you` both reconcile exactly.

**Consequence for the parser:** it can assert its own correctness. Recompute
`Total Cost` from the parsed rows and compare to the printed figure; any layout drift that
shifts a column will break the identity immediately rather than silently. This is a much
stronger integrity check than the monthly SoA reconciliation #5 proposed, and it runs daily.

**Consequence for the trade record:** fees are **daily/per-side**, so a per-fill fee is
necessarily an allocation, not a fact. Two of the six components don't even scale with a fill
(stamp duty is flat; the rest are proportional). Allocate the proportional 0.15% and 0.10% by
value, and treat stamp duty as a day-level cost — do not smear it across fills and call it a
per-fill number.

## Order vs fill — this mirrors IBKR

A useful symmetry with the IBKR finding in #4, where commission sits on the order and
price/quantity sit on the fill:

| | IBKR | Stockbit |
|---|---|---|
| Fill row key | `IB Execution ID` | none — position in table under a `REF #` |
| Order key | `IB Order ID` | `REF #` |
| Cost attaches to | the **order** (first fill carries it) | the **day + side** (coarser still) |
| Timestamp | `Trade Date` + `Trade Time` | trade **date** only, no time |

Both brokers therefore agree on the shape the trade record already assumes in
[#6](https://github.com/ajitimur/automatic-trading-journal/issues/6): an append-only Fill
ledger, with costs attached above the fill. Stockbit is simply the coarser of the two.

## Two facts that bite

- **The same document is delivered more than once, under different filenames.** The July SoA
  arrived twice (Mon 14:29 and Tue 00:39) with attachments named
  `<SUBACCOUNT-CODE>_soa300726_…` and `<ACCOUNT-NO>_soa300726_…` — a subaccount-code prefix
  in one, an account-number prefix in the other, for the same document and the same period.
  Dedupe on extracted content, never on filename or message-id.
- **Prices are not always integers, and that is not an error.** SoA prices carry up to 4
  decimals because they are weighted averages. The FUTR order above averages 352.5202. A
  parser that assumes integer rupiah prices will reject valid data.

---

## IBKR — still outstanding

Not obtainable without portal access, and it cannot be done from the mailbox: the daily
"Daily Activity Statement" email from `donotreply@interactivebrokers.com` is a **notification
only** — it carries no attachment and directs the reader to log in. This is confirmed against
the real emails, and it is precisely why #4 chose the Flex Web Service.

### The network gotcha — this will bite the daily job

**From this ISP, the Flex Web Service is unreachable, and it fails in the most misleading way
possible.** Telkom/IndiHome intercepts DNS for `interactivebrokers.com` and answers with its
own block-page address (`114.7.173.245/246`) instead of IBKR's Akamai edge. TLS then fails and
`curl` returns an **empty body** — no HTTP status, no `ErrorCode`, nothing that looks like a
Flex error. The first wizard run read that emptiness as a token problem and pointed at the
wrong fix entirely.

Verified: resolving the host over DNS-over-HTTPS and pinning it with `curl --resolve` returns
`http=200` and a well-formed `<FlexStatementResponse>` from the same machine, same moment.

Two consequences for the build:

- **The Flex client must not treat an empty response as a Flex error.** It is a transport
  failure and belongs in a different branch from the 1012/1015/1013 family — those need a
  human in the portal, this needs a network fix. Conflating them sends whoever is on call to
  the wrong place.
- **This is now an input to the undecided hosting question in #1.** If the daily job runs on
  this home connection it needs encrypted DNS or a VPN as a hard dependency. Cloud hosting
  sidesteps it entirely. That is a real point in favour of not self-hosting on this line.

The Akamai address rotated between two lookups minutes apart (`104.88.71.114` → `23.40.40.225`),
so any workaround must resolve fresh per run and never hardcode an IP.

### Running it

`scripts/sample-exports-wizard.sh` walks the remaining steps: build the Activity Flex Query
at `Level of Detail = Executions`, mint a long-lived token, fetch the XML, and probe it for
the three items #4 could not settle from documentation — the timezone of `Trade Time`, the
`IB Execution ID` format, and whether commission really lands on the first fill only.

## Still open after this round

- IBKR open items 1, 2, 3, 5 — need the Flex file (run the wizard).
- IBKR open item 4 — id stability across reruns; needs two runs on different days, then a diff.
- IBKR open item 6 — whether Trade Confirmation Flex lands earlier than Activity Flex.
- Stockbit 9 — pagination on a high-activity day.
- Stockbit 10 — whether a zero-activity day ever produces a TC.
- Stockbit 11 — Sekuritas account-opening T&C on automated processing.
