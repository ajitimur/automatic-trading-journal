# How trades get out of Stockbit

Research for [#5](https://github.com/ajitimur/automatic-trading-journal/issues/5). Date: 2026-08-12.

## Verdict

**PDF file drop — but the file arrives by itself.**

There is no retail API, no CSV, no XLSX, and no documented export button anywhere in
Stockbit's product. The only machine-addressable artefact carrying executed-trade detail is a
**PDF attachment on a daily email**. That is the intake format, and the journal will need a PDF
parser for the IDX book.

The important nuance, and the thing that makes this better than "manual file drop":

- **Retrieval can be fully unattended and is contractually clean.** Stockbit *pushes* the
  Trade Confirmation PDF to the user's inbox every trading day it has activity. Fetching it
  from that inbox (Gmail API / IMAP) never touches a Stockbit system, so it sits entirely
  outside the ToS clause that prohibits automated access to stockbit.com. Nothing needs to be
  scraped, logged into, or polled.
- **Only the confirm-and-enrich step is human**, which is what the map already settled on
  anyway ("intake by statement drop; confirm-and-enrich before anything commits").

So: the daily job can watch the mailbox, extract the PDF, parse it, and queue the fills for
confirmation. The human never drops a file manually unless they want to backfill.

The residual risk is entirely in the parser: a formatted PDF with no schema guarantee, which
will break silently on any layout change. Mitigation is discussed in
[Parser durability](#parser-durability).

---

## What Stockbit actually offers

### 1. Daily Trade Confirmation email — the primary source

Confirmed directly from the account owner's own inbox (primary evidence, not documentation):

| Property | Value |
| --- | --- |
| Sender | `no-reply@stockbit.com` |
| Subject | `Trade Confirmation <FULL NAME> DD-MM-YYYY` |
| Arrival | ~11:00–12:00 UTC, i.e. ~18:00–19:00 WIB, same trading day |
| Body | Boilerplate Indonesian only — **contains zero transaction data** |
| Attachment | exactly one, `application/pdf` |
| Attachment name | `TC_<account_no>-<YYYYMMDD>_<unix_epoch>.pdf` |

Example filename pattern (account number redacted):
`TC_<account>-<YYYYMMDD>_<epoch>.pdf`. The trailing integer is a Unix timestamp of PDF
generation, not a document identifier — two regenerations of the same day's TC produce two
different filenames.

Body text, verbatim:

> 👋 `<NAME>`,
> Berikut terlampir rekap transaksi harian kamu untuk tanggal 12 Agustus 2026.

("Attached is your daily transaction recap for 12 August 2026.")

**Sent only on days with activity.** Sampling a month of delivery showed ordinary trading days
with no TC at all. The absence of a TC is therefore "no fills", not "job failed". A monitoring
rule that alerts on a missing TC would false-positive constantly.

**Duplicate sends happen.** The same TC was observed delivered twice within half an hour, and a
monthly Statement of Account twice on the same day. **Import must be idempotent** — dedupe on
(account, trade date, stock, side, price, quantity) or on a content hash of the extracted
rows, never on message ID or filename, since both differ between the duplicates.

### 2. Monthly Statement of Account — the reconciliation source

| Property | Value |
| --- | --- |
| Sender | `no-reply@stockbit.com` |
| Subject | `Statement of Account <FULL NAME> DD-MM-YYYY` |
| Arrival | first few days of the following month |
| Attachment name | `<subaccount_code>_soa<DDMMYY>_<HHMMSS>.pdf` |

Also PDF-only. Public examples of the same document type confirm it is a period statement
covering cash movements, trading activity, and end-of-period holdings, with a
"deemed correct if no error reported within 48 hours" clause
([example](https://www.scribd.com/document/845743352/2374429-soa270225-011829)).

**Use it as a month-end reconciliation check, not as the intake path.** It is a summary
document; the TC is the fill-level record. Running "sum of TC-derived fills for month M ==
SoA trading activity for month M" is a cheap, high-value integrity assertion that catches a
silently-broken parser within 30 days.

### 3. In-app transaction history — read-only, no export

Stockbit's help centre documents a History screen on both web (Trading Area → History) and
mobile (Portfolio → History), with period filters *All Time / 1 month / 3 months / 1 year /
3 years / Custom*, plus a "Realized" toggle showing realised gain/loss
([help.stockbit.com](https://help.stockbit.com/id/article/history-bagaimana-cara-melihat-riwayathistory-transaksi-1uwoxlm/)).

The article documents **no download or export control of any kind** — no CSV, no Excel, no
PDF. Nor does the Portfolio Performance article
([help.stockbit.com](https://help.stockbit.com/id/article/performance-apa-itu-portfolio-performance-dan-bagaimana-cara-melihatnya-1m46xja/)).
A search of the help centre surfaces export/download articles for *deposit history* and
*withdrawal history*, but nothing for trade history.

One caveat the help centre itself raises: the Realized figure **excludes stamp duty and
datafeed fees**, so the app's own realised P&L is not the net number and should not be treated
as authoritative for the journal.

### 4. Undocumented private API — exists, do not use

Stockbit's own apps are backed by an undocumented HTTP API (`exodus.stockbit.com` is the
commonly-cited host). Third-party projects drive the brokerage side of it using a bearer token
lifted from browser storage — e.g. `perintis` instructs users to run
`localStorage.getItem("securitiesAccessToken")` in the browser console and paste the result
into a `.env`
([github.com/wiratmika/perintis](https://github.com/wiratmika/perintis)). Several other
repos scrape Stockbit's market-data endpoints
([github.com/basnugroho/indonesia-stocks-scraper](https://github.com/basnugroho/indonesia-stocks-scraper)).

This is **technically possible and contractually forbidden**. See
[Terms of Service](#terms-of-service-position). Do not design around it.

### 5. KSEI AKSes — independent cross-check, not an intake path

KSEI (the Indonesian central securities depository) gives every investor free access to the
AKSes portal, showing sub-account securities positions and **mutations for the last 30 days**,
plus downloadable monthly reports
([web.ksei.co.id](https://web.ksei.co.id/services/akses-facility)).

This is broker-independent and therefore a genuinely useful audit source — it will catch a
Stockbit-side error that reconciling TC against SoA cannot. But it is settlement-level
(T+2 movements of securities), not execution-level: no execution price, no fees, no
same-day fill breakdown. It cannot replace the TC. Treat as an optional manual annual/periodic
sanity check.

---

## Field list

**This is the one thing I could not establish from public sources, and it matters most.**

I could confirm the existence, naming, cadence, and delivery of the TC PDF from the account
owner's inbox, but I could not read the attachment content — the available tooling exposes
attachment metadata (filename, MIME type, size) but not attachment bytes, and no copies exist
in the linked Drive.

Publicly-indexed Stockbit TC PDFs exist (Scribd hosts a number of them, e.g.
[TC_0261596-20220411](https://www.scribd.com/document/584680130/TC-0261596-20220411),
[TC_2888321-20251106](https://www.scribd.com/document/952726986/TC-2888321-20251106-1762436052))
but Scribd serves only a prose summary to non-interactive fetches, not the document body. What
those summaries do confirm about the layout:

- Header carries the issuing entity (`PT. STOCKBIT SEKURITAS DIGITAL`), the client name, the
  account number, and the trade date.
- The document is split into **separate Sales and Purchase sections**, each with its own total
  (one example shows `Purchase Total: IDR 2,209,200.00` / `Sales Total: IDR 291,600.00`).
- Per-row detail includes the stock, quantity, and price; the summary block includes
  **V.A.T. / Levy and income-tax lines as named components**, plus a net settlement figure.
- A disclaimer: statement deemed correct unless an error is reported within 24 hours.

That is enough to know the shape but **not enough to write a parser against**. Everything in
the "must confirm" list below has to come from opening one real TC.

### Must be confirmed from inside the account

Open one TC PDF that contains a **partial fill** and one that contains **both a buy and a sell
of the same stock on the same day**, and record:

1. **Is there a text layer?** Run `pdftotext -layout` on it. If it produces clean columnar
   text, parsing is straightforward. If it produces nothing, the PDF is a rasterised image and
   the whole approach needs OCR — a materially worse position that should change the design.
2. **Is the PDF password-protected?** Several Indonesian brokers encrypt statement PDFs with
   the client's date of birth or ID number. Unattended parsing needs the password stored
   alongside the job. (No evidence either way; the account owner will know instantly.)
3. **Exact column headers**, verbatim, for the Sales table and the Purchase table.
4. **Quantity unit** — see [Lots vs shares](#lots-vs-shares).
5. **Partial fills** — see [Partial fills](#partial-fills-and-same-day-executions).
6. **Fee itemisation** — see [Fees and levies](#fees-and-levies).
7. **Is there a settlement date column** distinct from the trade date? IDX is T+2, and the
   journal wants trade date, so an SoA-style settlement date must not be mistaken for it.
8. **Does the TC show a time of execution?** Probably not (it is a daily recap), which matters
   for ordering multiple same-day fills.
9. **Is the PDF one page or does it paginate** when the day has many fills? Multi-page tables
   with repeated headers are a common parser break.
10. **Does a zero-activity day ever produce a TC** with an empty table, or is the email simply
    not sent? Evidence above says not sent, but a single counter-example changes the
    "missing TC == no fills" rule.

---

## Lots vs shares

IDX fixes one round lot at **100 shares** for all equity securities, uniformly, with no
per-stock exception — IDX Regulation No. II-A on the Trading of Equity Securities
([idx.co.id, Peraturan II-A](https://www.idx.co.id/media/10022/peraturan_ii_a_perdagangan_efek_bersifat_ekuitas.pdf)).
The 100-share lot has been in force since 6 January 2014 (previously 500).

The consequence for this journal:

- **The Stockbit order ticket is denominated in lots.** The user enters "5" meaning 500 shares.
  Whatever the human remembers about a trade will be in lots.
- **Money is denominated in shares.** Price is per share; gross value is
  `price × shares`, never `price × lots`.
- **Which unit the TC PDF uses is unconfirmed** and is item 4 on the must-confirm list. Both
  conventions are seen across Indonesian broker confirmations.

### Recommendation

**Store shares. Always. Canonically. One field, named `shares`.**

Do not store a `lots` field, and do not store a unit discriminator — a nullable unit column is
exactly the kind of thing that gets defaulted wrong once and silently corrupts a hundred
records. Derive lots for display as `shares / 100` at the presentation layer only.

Parser rule: multiply by 100 **only if** the TC is confirmed to be lot-denominated, and encode
that as a hard-coded constant in the Stockbit parser with a comment citing the confirmation,
not as configuration.

Cheap, robust validation that catches a wrong unit assumption on the first bad row:

```
assert abs(gross_value - price * shares) < 1.0   # IDR, i.e. sub-rupiah rounding only
```

If the TC gives both a quantity and a gross/net value — and the Scribd summaries indicate it
gives totals — this assertion is self-checking against the document itself and needs no
external truth. **A 100× unit error fails it immediately.** This should be a blocking parse
error, not a warning.

One trap: **odd lots**. Shares acquired via corporate action, or sold on the negotiated
market, can sit in non-multiples of 100. A validator that asserts `shares % 100 == 0` will
fire spuriously. Assert the value identity above instead; it holds for odd lots too.

---

## Partial fills and same-day executions

**Unconfirmed — must be established from a real document.** This is the single highest-risk
unknown, and the map already flags why: *"one mis-parsed partial fill poisons the exit
analysis."*

The two possibilities, and what each costs:

**(a) The TC lists each execution as its own row.** Then fill-level fidelity is available and
the journal can roll fills up into a position itself. This is the good case.

**(b) The TC aggregates per stock per side per day, showing a weighted-average price.** Then
fill-level detail is *permanently unavailable from this source* — the information does not
exist in the artefact and no parser can recover it.

Case (b) is not fatal but it constrains the design, and the constraint should be decided now
rather than discovered later:

- The strategy's scaling rule ("partial 1/3–1/2 on day 3 or 5") operates at **day
  granularity**, not fill granularity. A day's worth of selling at a weighted-average price is
  a faithful representation of "I sold a third on day 3".
- What is lost is intraday exit quality — whether the exit caught the high or the low of the
  day. Given the map already rules out intraday bars entirely ("daily is sufficient for every
  field in play"), **nothing downstream actually needs fill-level detail.**

**Recommendation regardless of which case holds:** make the journal's IDX unit of record a
**daily aggregate per (stock, side, trade date)**, not a fill. If the TC turns out to give
individual rows, aggregate them at parse time and keep the raw rows in a side table for
forensics. This makes the model identical under both cases, so the answer to this open question
cannot invalidate the schema — only enrich it. It also matches the SoA reconciliation
granularity.

Note the interaction with a same-day round trip: buying and selling the same stock on the same
day yields one row in the Purchase section and one in the Sales section. These must not be
netted. Key on (stock, **side**, date).

---

## Fees and levies

### What is charged

Stockbit's published commission
([help.stockbit.com](https://help.stockbit.com/id/article/transaksi-saham-berapa-biaya-trading-di-stockbit-sekuritas-1lbkyq9/)):

| Component | Buy | Sell |
| --- | --- | --- |
| Brokerage commission (stocks) | **0.15%** | **0.25%** |
| ETF / rights / warrants | 0.15% | 0.15% |
| Stamp duty (bea meterai) | Rp 10,000 per transaction where value > Rp 10,000,000 | same |
| Datafeed fee | monthly, banded by monthly transaction volume | — |

The 0.15% / 0.25% figures are quoted as **all-in**: they already absorb the IDX/KPEI/KSEI
transaction levy, VAT (PPN), and — on the sell side — the 0.1% final income tax (PPh Final)
on sale proceeds. The 10bp spread between the buy and sell rate is precisely that PPh Final.
ETF sells carry no PPh Final, which is why they stay at 0.15%.

### Baked in, or separate lines?

**Separate lines — fees are never baked into the price.** IDX equity prices move on a fixed
tick ladder and a fee-adjusted price would not sit on a valid tick. The public TC summaries
explicitly show V.A.T./Levy and income-tax entries as named components alongside a net
settlement amount.

What is **unconfirmed** is the *granularity* of the itemisation on the TC: whether it breaks
out commission / levy / VAT / PPh / stamp duty as five separate figures, or collapses them
into one "Fee" column plus a separate stamp duty line. Must-confirm item 6.

### Recommendation

Store **gross value, total fees, and net value** as three fields per aggregate row, and take
all three from the document rather than recomputing any of them. Do not attempt to reconstruct
fees from the percentage rates:

- Stamp duty is a Rp 10,000 **step function** at a Rp 10m threshold, so effective fee rate is
  discontinuous and depends on how the broker defines "per transaction" (per fill? per stock
  per day? per TC?) — itself unconfirmed.
- The datafeed fee is **monthly and volume-banded**, so it cannot be attributed to any single
  trade at all. It will appear on the SoA, not the TC. The journal should either ignore it or
  treat it as a periodic account-level cost; attributing it per-trade is arbitrary and would
  make trades non-comparable across months.
- The published percentages could change without notice.

Recompute-from-rate is fine as a **soft assertion** (flag if net differs from expected by more
than Rp 10,000 + rounding) but must never be the stored value.

This also directly answers the map's open question *"whether the record is net or gross"* for
the IDX side: the document gives both, so **store both** and let the analysis choose. Only
realised P&L needs to be net.

---

## Scheduling and unattended operation

| Stage | Unattended? | Notes |
| --- | --- | --- |
| Trade Confirmation arrives | Yes — Stockbit pushes it | ~18:00–19:00 WIB, activity days only |
| Fetch email + extract PDF | **Yes** | Gmail API or IMAP against the user's own mailbox |
| Parse PDF → fill rows | **Yes** | subject to text-layer and password questions above |
| Validate + dedupe | **Yes** | value identity assertion, content-hash dedupe |
| Confirm and enrich | **No — by design** | the map requires it; not a technical limitation |
| Monthly SoA reconciliation | **Yes** | assertion only; escalate to human on mismatch |

The daily background job the map already assumes can therefore own the whole intake pipeline
up to the confirmation queue. Recommended trigger: run the mailbox poll well after the WIB
close — say 14:00 UTC — so it never races the send, and make it idempotent so re-runs are
free.

The mailbox is a **Gmail account, not a Stockbit system**. Automating against it involves no
Stockbit credentials, no Stockbit endpoints, and no Stockbit rate limits. This is the entire
reason the flow is safe.

---

## Terms of Service position

### The clause

Stockbit's Terms ([stockbit.com/terms](https://stockbit.com/terms)) state, verbatim:

> You agree not to reproduce, retransmit, distribute, disseminate, sell, publish, broadcast or
> circulate the content received through Stockbit.com to anyone, including but not limited to
> others in the same company or organization, nor any use of **data mining, robots, spiders, or
> similar data gathering and extraction tools for any purpose without the express prior written
> consent of Stockbit**.

And on credentials:

> Registered Users must not allow any other person to use their user ID and password, and they
> must ensure that that user ID and password are kept confidential.

### What this rules out

🚫 **Do not build any of these:**

- Calling `exodus.stockbit.com` or any other private endpoint with a lifted
  `securitiesAccessToken`. This is "data gathering and extraction tools" applied to Stockbit
  content, without written consent. It is squarely prohibited.
- Headless-browser scraping of the History screen, the portfolio, or the web trading area.
  Same clause, and additionally it drives an authenticated brokerage session
  programmatically — the worst possible thing to have logged against a securities account.
- Storing Stockbit credentials in the journal for any purpose.

These are all *technically* achievable. They are contractually forbidden, and the downside is
not a warning email — it is a restricted or suspended brokerage account with real money and
open positions behind it. A trading journal is not worth that risk, and the PDF route gets
the same data.

### What this permits

✅ **Reading your own inbox is not covered by this clause at all.** The prohibition attaches to
"content received through Stockbit.com" and to extraction tools pointed at Stockbit. An email
Stockbit voluntarily sent to the user's own address, sitting in the user's own Gmail account,
processed by the user for the user's own personal records, involves:

- no access to a Stockbit system,
- no Stockbit credential,
- no redistribution (the "reproduce/retransmit/distribute" language is about circulating
  content *to anyone* — a private single-user journal circulates it to no one),
- no load on Stockbit infrastructure.

This is the recommended route and it is clean.

### One residual caveat

The clause above is from **stockbit.com** (the platform/community). The brokerage relationship
is governed by a separate document, PT Stockbit Sekuritas Digital's *Syarat dan Ketentuan
Pembukaan Rekening Efek* (account-opening terms and conditions), accepted during onboarding.
I could not retrieve an authoritative copy of the current version from a primary source — it
is not published at a stable public URL, and the copies in circulation are third-party
uploads of uncertain vintage
([Scribd copy](https://www.scribd.com/document/708788659/Syarat-Dan-Ketentuan-Pembukaan-Rekening-Stockbit-Sekuritas-Digital);
an academic analysis of it exists at
[Undip repository, 2024](https://eprints2.undip.ac.id/id/eprint/26297/)).

**Must-confirm item 11:** the account owner has this document (or can retrieve it in-app) and
should skim it for any clause restricting automated processing of statements or requiring
statements be kept confidential. I consider it very unlikely to prohibit parsing one's own
statements — that would make ordinary tax preparation non-compliant — but it should be
eyeballed rather than assumed.

---

## Consolidated must-confirm list

Everything below requires access to the inside of the account. Each is cheap to answer and
each one is load-bearing for the parser design.

| # | Question | Blocks |
| --- | --- | --- |
| 1 | Does `pdftotext -layout` yield a clean text layer from a TC? | whole approach; OCR fallback if not |
| 2 | Is the TC PDF password-protected? | unattended parsing |
| 3 | Exact column headers of the Sales and Purchase tables | parser |
| 4 | Quantity column: lots or shares? | normalisation (100×) |
| 5 | Partial fills: separate rows or daily weighted average? | fill vs daily-aggregate model |
| 6 | Fees: itemised into commission/levy/VAT/PPh/stamp, or one total? | fee fields |
| 7 | Is there a settlement date column distinct from trade date? | date semantics (T+2) |
| 8 | Is an execution time shown? | ordering same-day fills |
| 9 | Does the TC paginate on high-activity days? | parser robustness |
| 10 | Does a zero-activity day ever produce a TC? | "missing == no fills" rule |
| 11 | Does the Sekuritas account-opening T&C restrict automated statement processing? | ToS confidence |

Answering 1, 4, 5, and 6 unblocks a first parser. The rest are hardening.

---

## Parser durability

The honest risk assessment: **this parser will break, and the question is only whether it
breaks loudly.**

A PDF layout is not a contract. Stockbit can re-theme its statements at any time with no
notice and no versioning, and the failure mode of a naive positional parser is not an
exception — it is plausible-looking wrong numbers. Given that the map's whole premise is
using this data to grade the user's own discipline, wrong numbers that look right are worse
than no numbers.

Three cheap defences, in priority order:

1. **Assert the value identity on every row** (`gross ≈ price × shares`, `net ≈ gross ± fees`).
   Self-checking against the document, no external truth needed. Catches unit errors, column
   misalignment, and decimal-separator mishaps. **Blocking, not a warning.**
2. **Match on column headers, not column positions.** Locate the header row by its text and
   derive field offsets from it. Survives added columns and re-ordering; fails loudly if a
   header is renamed, which is the correct behaviour.
3. **Keep the source PDF, permanently.** Store it alongside the parsed rows. A re-parse of the
   archive is the only recovery path when a bug is found six months later, and it costs
   nothing — these are small files, roughly one per trading day.

Add to that the monthly SoA reconciliation described above, and a break is detected within at
most 30 days rather than at the point someone questions a P&L figure.

One further note: the confirm-and-enrich step the map already mandates is, conveniently, also
the parser's safety net. A human eyeballing each day's fills before they commit will notice a
100× quantity or a swapped buy/sell long before any automated check would. That step should
be treated as load-bearing for correctness, not merely as a UX nicety — which argues for
showing the parsed values next to the source PDF page in the confirmation UI.

---

## Sources

Primary evidence: direct inspection of Stockbit's delivery to the account owner's own mailbox —
cadence, sender, subject format, attachment filename conventions, PDF-only delivery, duplicate
sends, and activity-day-only sending. Specifics identifying the account or its trading days are
deliberately omitted from this document; re-derive them from the mailbox if needed.

Published sources:

- Stockbit Terms of Service — https://stockbit.com/terms
- Stockbit help, transaction history — https://help.stockbit.com/id/article/history-bagaimana-cara-melihat-riwayathistory-transaksi-1uwoxlm/
- Stockbit help, trading fees — https://help.stockbit.com/id/article/transaksi-saham-berapa-biaya-trading-di-stockbit-sekuritas-1lbkyq9/
- Stockbit help, stamp duty — https://help.stockbit.com/id/article/apa-itu-biaya-bea-meterai-p08y2z/
- Stockbit help, portfolio performance — https://help.stockbit.com/id/article/performance-apa-itu-portfolio-performance-dan-bagaimana-cara-melihatnya-1m46xja/
- IDX Regulation No. II-A, equity trading (100-share round lot) — https://www.idx.co.id/media/10022/peraturan_ii_a_perdagangan_efek_bersifat_ekuitas.pdf
- KSEI AKSes facility — https://web.ksei.co.id/services/akses-facility
- `perintis` (undocumented brokerage API via `securitiesAccessToken`) — https://github.com/wiratmika/perintis
- `indonesia-stocks-scraper` (Stockbit market-data scraping) — https://github.com/basnugroho/indonesia-stocks-scraper
- Example TC PDFs (summaries only) — https://www.scribd.com/document/584680130/TC-0261596-20220411 and https://www.scribd.com/document/952726986/TC-2888321-20251106-1762436052
- Example SoA PDF (summary only) — https://www.scribd.com/document/845743352/2374429-soa270225-011829
- Stockbit Sekuritas account-opening T&C (third-party upload, vintage unverified) — https://www.scribd.com/document/708788659/Syarat-Dan-Ketentuan-Pembukaan-Rekening-Stockbit-Sekuritas-Digital
