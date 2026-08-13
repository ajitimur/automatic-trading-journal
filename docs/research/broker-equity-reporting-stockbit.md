# Broker equity reporting — Stockbit / IDX book

Research for the per-book **Equity Snapshot** (the denominator of `risk % = (entry_price − stop) × size ÷ equity`),
readable at arbitrary past dates back to the **July 2026** backdating floor.

Scope constraint taken as settled by earlier tickets and **not** re-opened here: retrieval must be
**unattended — no browser automation, no scraping, no 2FA, no stored Stockbit credentials**. Stockbit's Terms
forbid automated extraction (quoted below), so the undocumented `exodus.stockbit.com` API and headless-browser
scraping are out of scope and are not discussed as options.

Every claim below is tagged **[documented]** (stated by the source that owns the fact),
**[inferred]** (my reasoning from documented facts), or **[check]** (must be verified against the owner's own
documents/inbox — a one-line check is given).

Research date: 2026-08-13. Regulations verified in force as of that date.

---

## 0. The governing rule for what a monthly customer statement must contain

The operative regulation is **POJK No. 13 Tahun 2025** (Pengendalian Internal dan Perilaku Perusahaan Efek yang
Melakukan Kegiatan Usaha sebagai Penjamin Emisi Efek dan Perantara Pedagang Efek), set 5 June 2025, promulgated
11 June 2025, in force six months after promulgation → **11 December 2025**, therefore in force for the July 2026
statement. It revokes POJK 3/POJK.04/2020, 4/POJK.04/2020 and 50/POJK.04/2020 (Pasal 120–121).
Source (PDF, OJK): <https://ojk.go.id/id/regulasi/Documents/Pages/POJK-13-Tahun-2025-Pengendalian-Internal-dan-Perilaku-Perusahaan-Efek-yang-Melakukan-Kegiatan-Usaha/POJK%2013%20Tahun%202025%20Pengendalian%20Internal%20dan%20Perilaku%20Perusahaan%20Efek%20yang%20Melakukan%20Kegiatan%20Usaha%20Sebagai%20Penjamin%20Emisi%20Efek%20dan%20Perantara%20Pedagang%20Efek.pdf>

Its customer-statement clause is a verbatim carry-over of the old **Peraturan Bapepam-LK No. V.D.3** (Lampiran
Keputusan Ketua Bapepam-LK No. Kep-548/BL/2010 tanggal 28 Desember 2010), angka 10 huruf c butir 3) and 4).
V.D.3 source (PDF, OJK): <https://www.ojk.go.id/Files/regulasi/pasar-modal/bapepam-pm/pe-wpe-pi-rd/perusahaan-efek/V.D.3.pdf>

Verbatim (POJK 13/2025, Lampiran, angka 10 huruf c):

> 3. laporan Rekening Efek harus memuat posisi portofolio Efek nasabah pada tanggal laporan, dan dikirimkan
>    kepada nasabah paling lambat pada hari ke-10 (kesepuluh) setiap bulan, termasuk kegiatan transaksi nasabah
>    selama 1 (satu) bulan; dan
> 4. transaksi yang termuat dalam laporan Rekening Efek mencakup:
>    a) transaksi yang telah dilaksanakan;
>    b) jumlah dividen, saham bonus, bunga, hak memesan Efek terlebih dahulu, dan hak lainnya; dan
>    c) penarikan atau penyetoran dana dan/atau Efek;

**What this establishes [documented]:**

- A monthly *laporan Rekening Efek* is **mandatory**, must be sent to the customer by the **10th of each month**,
  and must contain the **portfolio position (`posisi portofolio Efek`) as at the statement date**, plus one month
  of transaction activity including dividends, bonus shares, interest, rights (HMETD), and cash/securities
  deposits and withdrawals.
- **Valuation at market price is NOT required.** The words `nilai pasar`, `harga pasar`, `penilaian` and `valuasi`
  do **not appear anywhere** in either V.D.3 or POJK 13/2025 (grepped over the full text of both PDFs). The rule
  mandates *position*, i.e. what and how much is held — not what it is worth. A statement that lists
  "1,000 shares of XXXX" and nothing else is fully compliant.
- The record-keeping clause immediately above (angka 10 huruf c butir 2) requires per-transaction recording of
  `tanggal transaksi, uraian transaksi, jumlah dana, jumlah Efek, kurs transaksi` — again quantities and
  transaction prices, never a mark-to-market.

**Consequence [inferred]:** even a perfectly compliant Stockbit monthly statement is *not guaranteed* to give a
portfolio market value. Regulation buys us at most a quantity-level holdings table. Anything more is Stockbit's
own choice of format and must be checked empirically, not assumed.

---

## 1. Does the Stockbit monthly Statement of Account contain a securities-position section?

**Status: [check] — undetermined, and the help centre cannot settle it.**

- Stockbit's help centre (<https://help.stockbit.com/id/>) has **no article at all** describing the Statement of
  Account or the daily Trade Confirmation email. I enumerated the complete article list from the help-centre
  sitemaps (`https://help.stockbit.com/sitemap-articles-1.xml`, `-2.xml`) and filtered for
  *statement / laporan / konfirmasi / confirm / pajak / portofolio / e-mail / rdn / saldo*; the only
  statement-like documents covered are the **Tax Report** (§2.2) and the in-app **Portfolio Performance** feature
  (§2.3). **[documented]** — this is a documented *absence*, which is itself the finding: there is no vendor
  description of the SoA to lean on, and none to be misled by.
- Regulation (§0) requires the monthly statement to carry `posisi portofolio Efek pada tanggal laporan`, so a
  holdings section **should** exist somewhere in the document to be compliant. **[inferred]** — but the July 2026
  SoA is already known empirically to be a cash ledger on the page examined, so the holdings section, if present,
  is on the pages not yet read.
- Even if a holdings table exists, §0 means it may carry **quantity only, with no market valuation**. **[inferred]**

**Check to run:** open the July 2026 Statement of Account PDF and read pages 2–5; note (a) whether any page
carries a per-symbol holdings table, and (b) if it does, whether that table has a price/market-value column or
only quantity/lots.
*Secondary check:* if a holdings table exists, note whether it also carries a grand total combining securities
value with the cash ending balance — that total, if present, is the Equity Snapshot for that month-end.

---

## 2. Is portfolio market value obtainable through an unattended, ToS-permitted channel?

### 2.0 The ToS boundary (for the record)

Stockbit's Terms, <https://stockbit.com/terms>, verbatim:

> You agree not to reproduce, retransmit, distribute, disseminate, sell, publish, broadcast or circulate the
> content received through Stockbit.com to anyone … nor any use of data mining, robots, spiders, or similar data
> gathering and extraction tools for any purpose without the express prior written consent of Stockbit …

**[documented]** — this is the clause that keeps automated retrieval out of scope. Reading one's own inbox is not
covered by it; automating Stockbit's own surfaces is.

### 2.1 Emailed Stockbit documents — daily Trade Confirmation

**Ruled out. [documented]** — established empirically by an earlier ticket: one row per fill
(`REF # · Board · Share · Lot · Quantity · Price · Buy · Sell`), fees itemised per side per day, **no portfolio
value and no holdings section**. Nothing in the present research changes that.

### 2.2 Emailed Stockbit documents — the **Tax Report** (annual e-Statement)

This is the one Stockbit document that is *delivered by email* and is *documented* to contain asset values.

Help-centre article "Bagaimana Cara Melaporkan Pajak Saham di SPT Online - Coretax?",
<https://help.stockbit.com/id/article/bagaimana-cara-melaporkan-pajak-saham-di-spt-online-coretax-16odghf/>,
verbatim:

> Dokumen ini mencakup informasi penghasilan dari investasi, pajak yang telah dipotong, serta **nilai harta/aset
> investasi**.
> … Melalui Web: Klik Trading Area → Klik E-Statement → Klik Tax Report → Pilih **Periode Pajak** → Klik **Kirim
> Ke Email** … Dokumen untuk pelaporan SPT Saham akan dikirimkan ke email yang terdaftar di akun Stockbit kamu.

and, for the SPT filing itself, "Isi **Harga perolehan** dan **Nilai Saat ini** sesuai Tax Report" — i.e. the Tax
Report carries both cost basis and a current/period-end value per the Coretax Harta (L-1) section.

An earlier Stockbit article for the SPT 2022 edition,
<https://snips.stockbit.com/edukasi/lapor-spt-2022-lebih-mudah>, describes the same document as containing
"Total harta dan kas yang terdapat di akun Stockbit" and "Portofolio saham di akun Stockbit tertanggal hingga
31 Desember 2022", with "informasi portofolio yang mencerminkan harga per akhir tahun (31 Desember 2022)".

**Assessment:**
- **[documented]** It contains cash + stock portfolio priced at **year end** — structurally, an equity figure.
- **[documented]** It is delivered **by email**, but only after a manual in-app/web request per tax period
  (`Pilih Periode Pajak → Kirim Ke Email`). It is not pushed on a schedule.
- **[inferred]** Therefore: **not unattended** (a human must request it), and **annual granularity, 31 December
  only**. Against a July 2026 floor and arbitrary entry dates, one datapoint per year is useless as a series. It
  is at best a once-a-year reconciliation anchor.
- **[check]** The `E-Statement` menu may offer report types besides `Tax Report` (the UI path implies a
  submenu). **Check:** open Stockbit → Profil → E-Statement and list every report type offered and every period
  selector each one exposes; if any type yields a month-end or arbitrary-date portfolio valuation, that changes
  the verdict in §6.

### 2.3 Stockbit in-app **Portfolio Performance** — has a Total Equity series

<https://help.stockbit.com/id/article/performance-apa-itu-portfolio-performance-dan-bagaimana-cara-melihatnya-1m46xja/>
— "Total Equity: Menampilkan nilai total equity dari portofolio saham kamu", with data "dimulai dari
1 Januari 2024", available on mobile, web and desktop.

<https://help.stockbit.com/id/article/performance-bagaimana-cara-menghitung-total-equity-return-pada-portfolio-performance-1trirrg/>
— "Menampilkan perubahan nilai total portofolio saham kamu (Total Equity) beserta total realized dan unrealized
gain kamu **setiap hari atau setiap bulan**", and the return calculation explicitly neutralises deposits and
withdrawals ("penyesuaian untuk memastikan deposit dan withdrawal tidak memengaruhi kinerja portofolio yang
sebenarnya").

**Assessment:**
- **[documented]** Stockbit itself maintains a **daily** Total Equity series starting **1 January 2024** — which
  comfortably precedes the July 2026 floor.
- **[documented]** It is a UI feature. The help centre documents no export and no email delivery.
- **[inferred]** This is therefore the **best-quality equity series that exists**, and it is reachable **only by
  hand-entry**: the owner reads a number off the app. Automating it would require exactly the scraping the ToS
  forbids, so it stays hand-entry.
- **[check]** Whether the UI actually exposes a *readable numeric value at an arbitrary past date* (e.g. hovering
  a chart point for 14 Oct 2026) or only endpoint values for preset ranges. **Check:** open Portfolio Performance,
  set a custom range ending on some past date, and confirm whether an exact Total Equity number for that date can
  be read off (chart tooltip or table row) rather than only a percentage return.

### 2.4 **KSEI AKSes** (`akses.ksei.co.id`)

Primary sources: KSEI education page <https://web.ksei.co.id/education/akses-facility>, and KSEI's own user guide
**"Panduan Pengguna Web AKSes - Portofolioku"** (issued 21 April 2020, 40 pp),
<https://web.ksei.co.id/files/Panduan_Pengguna_Web_AKSes_-_Portofolioku.pdf>.

**What AKSes reports [documented]:**

- Consolidated balances across all brokers/custodians, broken down into Ekuitas, Reksa Dana, Obligasi, **Kas**,
  and Efek lainnya — i.e. securities *and* cash in one view (guide §A).
- It **does value equities at market**. Guide §A.2, verbatim:
  > b. Total investasi: total nominal nilai kepemilikan Efek ekuitas dalam rupiah (IDR). Nilai ini diperoleh dari
  > jumlah seluruh perkalian **harga penutupan terakhir (last closing price)** dengan jumlah lembar (quantity)
  > Efek yang dimiliki

  and §A.1 states the formula outright: `∑(last closing price × quantity)`. Cash is `∑ saldo IDR` per bank.
  **This is the same construction as the reconstruction alternative in §4** — KSEI computes exactly
  `quantity × close`, plus cash.
- Per-symbol values, per-broker subtotals, and a grand total in IDR.

**History depth [documented]:** the as-of date selector is capped.
  > a. Tanggal kepemilikan: tanggal kepemilikan saldo Efek/dana, dapat dipilih sampai dengan **90 (sembilan puluh)
  > hari terakhir**. Secara default akan menampilkan tanggal hari ini.

  The mutation search is capped the same way: "tanggal mutasi, dapat diisi rentang tanggal transaksi hingga
  90 (sembilan puluh) hari terakhir". Note KSEI's education page states a **30-day** figure for real-time
  holdings/mutation data ("hingga 30 hari terakhir") — the two KSEI pages disagree; the guide is the more specific
  of the two on the as-of selector. Either way the ceiling is **months, not years**.

**Does KSEI email a periodic statement? [documented] — for equities, no.**
The guide's report menu (`Portofolioku > Laporan`, guide §H) is titled **"Laporan Notifikasi Reksa Dana"** and
offers exactly two report types: **Laporan Bulanan Reksa Dana** (mutual-fund monthly report) and **Laporan Mutasi
Reksa Dana** (mutual-fund movement report). Both are **mutual funds only**. There is **no equity monthly report**
and no equity statement in the AKSes Laporan menu. The generally-cited "AKSes monthly statement" in third-party
write-ups is this mutual-fund report, and does not apply to an IDX equity book.
Mutation data *can* be downloaded as CSV or PDF from within the logged-in session ("Anda dapat mengunduh
(download) data mutasi dalam format berkas (file) CSV atau PDF"), but that is a movement log, not a valuation,
and still requires the login.

**Classification [inferred]:** AKSes is a **credentialed web login the human performs manually → hand-entry**.
It is not an unattended channel. Its 90-day ceiling also disqualifies it for backdating to July 2026 from
late 2026 onward. It is, however, an excellent **spot-check / reconciliation** tool for *today*, and — because it
is broker-independent — a way to catch positions held outside the journal.

**Check:** KSEI is known to send mutation notification emails from `akses@ksei.co.id`. Search the inbox for
`from:ksei.co.id` and record what actually arrives: if only "Notifikasi Pemindahan Efek"-type movement alerts,
they carry no portfolio value and change nothing; if anything periodic with a valuation arrives, re-open this
section. (Third-party bank pages assert such emails exist; I found no KSEI-owned page documenting an equity
notification email, so treat their existence and content as unverified.)

### 2.5 Summary table

| Channel | Gives market value? | Unattended? | ToS-permitted? | Granularity / depth |
|---|---|---|---|---|
| Daily Trade Confirmation email | No | Yes | Yes | per fill, daily |
| Monthly Statement of Account email | Cash yes; securities **unknown**, valuation not mandated | Yes | Yes | month-end |
| Stockbit Tax Report (E-Statement) | Yes (cost + current value) | **No** — manual request, then emailed | Yes | **annual, 31 Dec** |
| Stockbit Portfolio Performance UI | Yes (Total Equity) | **No** — hand-entry | UI reading only | **daily, from 2024-01-01** |
| KSEI AKSes web login | Yes (`∑ close × qty` + cash) | **No** — hand-entry | Yes (manual use) | as-of date, **last 90 days** |
| KSEI emailed statement | n/a for equities — mutual funds only | — | — | — |

---

## 3. History depth against the July 2026 floor

**[documented / inferred as marked]**

- **Monthly SoA:** month-end only, one figure per month, available for every month the account has existed —
  provided the securities section exists at all (§1, unresolved). Against a July 2026 floor this is **month-end
  granularity**, ~13 points to cover Jul 2026 → Aug 2026 onwards. **[inferred from the cadence mandated in §0.]**
- **Stockbit Portfolio Performance:** **daily**, from **1 January 2024** **[documented]** — the only channel whose
  native granularity matches an arbitrary-date query, and it is hand-entry.
- **Tax Report:** **one point per year, 31 December** **[documented]**.
- **KSEI AKSes:** as-of any date within the **last 90 days** only **[documented]**; effectively
  point-in-time-now. It cannot reach July 2026 from any date after ~October 2026.
- **Daily Trade Confirmation:** full daily history of *fills*, no equity **[documented, prior ticket]**.

**The load-bearing point:** the only *unattended* channel with pre-floor reach is the SoA, and it is at best
**month-end**, not daily. A month-end series interpolated to an entry date is an approximation whose error is
whatever the portfolio did intramonth — for a risk-% denominator that is usually tolerable, but it is not the
same answer as a daily series and should not be presented as one.

---

## 4. The reconstruction alternative — what it gets right, and what makes it wrong

The proposal: `equity(t) = cash(t) + Σ_symbol holding_qty(t) × close(symbol, t)`, with `cash(t)` from the SoA
ledger's running `Ending Balance`, `holding_qty(t)` accumulated from the append-only Fill ledger, and `close`
from the committed yfinance `.JK` split-adjusted daily OHLCV source.

**Not endorsed here** — the decision belongs to a later ticket. What follows is only what it can and cannot know.

### What it would get right

- **The formula is the right formula.** KSEI computes investor portfolio value with literally this construction:
  `∑(last closing price × quantity)` plus cash (§2.4). The approach is not exotic; it is the market
  infrastructure's own definition. **[documented]**
- **Granularity.** It produces a value for **every trading day**, not month-end — the only derivable channel that
  can. **[inferred]**
- **Trades the journal knows about.** Every buy and sell recorded in the Fill ledger moves quantity correctly, and
  the SoA cash ledger independently reflects the same trades' cash legs, giving a cross-check. **[inferred]**
- **Splits.** yfinance `.JK` closes are split-adjusted, so a raw historical close will not misprice a post-split
  holding *provided* the Fill-ledger quantities are adjusted on the same basis. **[inferred — and note the trap:
  mixing split-adjusted prices with unadjusted historical quantities silently misvalues every pre-split position.]**

### What would make it wrong — the leakage terms

Each of these breaks either the cash term or the quantity term. Listed with the mechanism, not just the name.

1. **Deposits and withdrawals.** *Cash term — covered, if and only if the SoA is the cash source.* RDN top-ups and
   withdrawals appear in the SoA ledger as Db/Cr rows and are already inside the running `Ending Balance`. If
   instead cash were reconstructed from fills alone, every deposit would be missing and equity would be wrong by
   the full amount, permanently. **[inferred]** **Check:** confirm deposit and withdrawal rows are present and
   signed correctly in a month where one occurred.
2. **Dividends received.** *Cash term.* Cash dividends land in the RDN and therefore appear in the SoA
   `Ending Balance` — so they are captured by the cash side. They are **not** in the Fill ledger, so any attempt
   to reconstruct cash from fills would drop them. §0 confirms dividends are a mandated line item of the statement
   (`jumlah dividen`). **[documented + inferred]**
3. **Stock dividends / bonus shares (saham bonus).** *Quantity term — broken.* These increase share count with no
   fill and no cash movement. The Fill ledger will never see them; holdings will be understated from the ex-date
   onward, and permanently. Mandated statement line item per §0. **[documented + inferred]**
4. **Rights issues (HMETD) and warrants.** *Both terms — broken.* Exercising rights consumes cash (visible in the
   SoA, so cash stays right) but creates shares with no fill (quantity understated). Unexercised rights and
   warrants are themselves tradeable instruments with their own tickers that the journal's symbol universe likely
   does not carry; their value simply vanishes from the estimate. Mandated statement line item per §0
   (`hak memesan Efek terlebih dahulu, dan hak lainnya`). **[documented + inferred]**
5. **Positions predating the July 2026 floor.** *Quantity term — the single largest hole.* The Fill ledger starts
   at the floor, so any position opened before it has an **unknown opening quantity** and is valued at zero. Every
   reconstructed equity figure is then understated by the entire value of the legacy book, and the error persists
   until each legacy position is fully sold (at which point the sale proceeds appear as cash and equity *jumps* by
   an amount the model cannot explain). This is not noise; it is a level shift. **[inferred]** **Check:** open
   AKSes today (or the Stockbit portfolio screen) and list every symbol held whose first fill is not in the Fill
   ledger — that set is the opening-balance gap, and it must be seeded manually for any reconstruction to work.
6. **IPO / e-IPO allotments.** *Quantity term.* Shares allotted at IPO arrive without a Bursa fill row; whether
   they surface in the Fill ledger at all depends on how e-IPO allotments are represented in the daily Trade
   Confirmation. Cash is debited (so the cash side stays right), which makes the error one-directional: cash
   drops, shares never appear, equity understated. **[inferred]** **Check:** if any e-IPO allotment occurred after
   the floor, look for its REF row in that day's Trade Confirmation; if absent, IPO allotments are a confirmed
   quantity leak.
7. **Anything bought outside the journal / in another portfolio.** Stockbit supports **multiple portfolios** with
   inter-portfolio share and cash transfers
   (<https://help.stockbit.com/id/article/multiple-portfolio-bagaimana-cara-transfer-dana-transfer-saham-jika-memiliki-multiple-portfolio-lr4klk/>),
   and share transfers in from another broker are a supported flow. Any of these moves quantity with no fill and
   no cash movement in this book — the reconstruction cannot see it at all. **[documented + inferred]**
8. **Corporate actions that change share count without a trade — splits and reverse splits.** Covered only if the
   Fill-ledger quantities are re-based consistently with yfinance's adjusted series; otherwise off by the split
   ratio. **[inferred]**
9. **Delisted, suspended and illiquid symbols.** yfinance will return a stale close, no close, or drop the symbol
   entirely. A suspended stock is carried at its last traded price for as long as the suspension lasts, which may
   be wildly wrong; a delisted one silently disappears from the valuation. **[inferred]**
10. **Trading limit / margin and unsettled T+2 cash.** The SoA `Ending Balance` is an RDN cash balance on a
    trade-date ledger with Due Date columns; whether it nets unsettled obligations, and whether any trading-limit
    debt is reflected, determines whether `cash + holdings` is *equity* or *gross assets*. Stockbit distinguishes
    "Trading Balance" from RDN balance
    (<https://help.stockbit.com/id/article/transaksi-saham-apa-perbedaan-trading-balance-dan-rdn-1v37rji/>).
    **[documented that they differ; inferred that this matters]** **Check:** in the July SoA, pick a day with a buy
    settling T+2 and confirm whether `Ending Balance` on trade date already reflects the debit or only on due date.
11. **Fees, stamp duty and datafeed charges.** These hit cash and are in the SoA, so the cash term absorbs them —
    but note Stockbit's own realized-gain figure excludes some of them ("Total realized tidak termasuk biaya
    materai dan biaya datafeed",
    <https://help.stockbit.com/id/article/history-bagaimana-cara-melihat-riwayathistory-transaksi-1uwoxlm/>), so
    reconstruction and Stockbit's displayed numbers will not tie out to the rupiah. **[documented + inferred]**

### The shape of the error

Leakages 3–7 all push the **same direction**: quantity understated → **equity understated** → **risk % overstated**.
That is the conservative direction for a risk denominator, which is a mild comfort, but the magnitude is unbounded
and dominated by item 5 (pre-floor positions). **[inferred]**

---

## 5. Currency and units facts a consumer needs

- **Currency: IDR** throughout. AKSes states values are "dalam rupiah (IDR)" and totals cash as `∑ saldo IDR`
  (<https://web.ksei.co.id/files/Panduan_Pengguna_Web_AKSes_-_Portofolioku.pdf>). **[documented]**
- **No decimals in practice.** IDX equity prices trade on whole-rupiah tick sizes (*fraksi harga*) under
  **Peraturan Bursa Nomor II-A tentang Perdagangan Efek Bersifat Ekuitas** (current version Kep-00055/BEI/03-2023).
  *Caveat on sourcing:* `idx.co.id` serves 403 to non-browser clients, so I could not pull the rule PDF's tick
  table verbatim — the rule identifier is reliable, the exact per-band tick figures are **[check]** if they ever
  matter. They do not matter for equity: an equity figure is a rupiah total, and **storing IDR as an integer is
  safe**; storing it in a float32 or a 2-dp decimal sized for USD is not — account totals run to 9–10 significant
  digits. **[inferred]**
- **Lot = 100 shares.** IDX defines the *Satuan Perdagangan* (round lot) as 100 equity securities, per Peraturan
  Nomor II-A. **[documented via the rule; the PDF text itself is bot-blocked — see caveat above.]**
- **Both units appear side by side in the source documents.** The daily Trade Confirmation carries **both** a
  `Lot` and a `Quantity` column (established empirically, prior ticket). Whichever is chosen, the other must be
  derivable and the choice recorded per field — a silent 100× is the most likely unit bug in this book.
  **[inferred]** **Check:** on any Trade Confirmation row, confirm `Quantity = Lot × 100` holds; if any row
  violates it (odd-lot / negotiated-market fills), the Fill ledger must store `Quantity`, not `Lot`.
- **Odd lots exist** and are traded on a separate board — Stockbit documents buying/selling odd lots
  (<https://help.stockbit.com/id/article/transaksi-saham-bagaimana-cara-belijual-dan-sembunyikan-saham-odd-lot-dari-portfolio-132yk1u/>).
  So `Quantity` is not always a multiple of 100. **[documented]**
- The Trade Confirmation's `Board` column distinguishes market boards (regular / negotiated / cash);
  negotiated-market fills do not follow the regular-market tick and lot conventions. **[inferred]**

---

## 6. Verdict

**For the IDX book: hand-entry only.**

Plainly: there is **no unattended, ToS-permitted channel that delivers a portfolio market value at an arbitrary
past date**, and none that delivers a daily one at all.

The reasoning, compressed:

- The only channels that *push* documents unattended are the daily Trade Confirmation (no equity, settled) and
  the monthly Statement of Account. The SoA is regulation-bound to carry *positions*, but **not valuations**
  (§0) — so the best case it can offer is a **month-end quantity table** that still needs external pricing, and
  whether it carries even that is unverified (§1).
- Every channel that *does* carry a real market value — Stockbit Portfolio Performance (daily, from 2024),
  Stockbit Tax Report (annual, 31 Dec), KSEI AKSes (`∑ close × qty` + cash, last 90 days) — requires a human at a
  credentialed session. The Tax Report is emailed, but only after a manual request and only annually; that is
  hand-entry with an email delivery step, not automation.
- Reconstruction is *derivable* in form but not in fact: item 5 of §4 (positions predating the July 2026 floor,
  opening balance unknown) alone makes every reconstructed figure wrong by an unbounded level shift until those
  positions are seeded by hand — which is, again, hand-entry.

**Suggested cadence of hand entry:** **at each entry date** is the correct default — the Equity Snapshot is a
per-trade denominator, and the owner is already in the app at the moment of entry, so reading Total Equity off
Portfolio Performance costs one glance and is exact for that date. A **monthly** snapshot (on receipt of the SoA,
by the 10th per §0) is the sensible fallback and the right backfill cadence for the July 2026 → present gap, at
the cost of interpolation error for entries made mid-month. **Per-trade is not required for correctness of
historical rows** — a monthly series carried forward to the next entry date is a defensible denominator — but
per-trade removes the interpolation question entirely and should be the going-forward mode.

**If §1's check comes back positive** — i.e. the SoA pages 2–5 do carry a holdings table *with* market values and
a combined total — the verdict upgrades to **derivable-with-caveats** at **month-end granularity**, unattended,
with the caveats being interpolation between month-ends and the leakage terms of §4 for any intramonth estimate.
That single check is the highest-value next action in this whole area.

---

## Open empirical checks, collected

1. July 2026 SoA pages 2–5: is there a holdings table? does it carry market value or quantity only? is there a
   combined securities + cash total? (§1 — highest value)
2. Stockbit → Profil → E-Statement: what report types exist besides Tax Report, and what period selectors? (§2.2)
3. Portfolio Performance: can an exact Total Equity number be read for an arbitrary past date? (§2.3)
4. Inbox `from:ksei.co.id`: what does KSEI actually send, and does any of it carry a valuation? (§2.4)
5. Which currently-held symbols have no first fill in the Fill ledger — the pre-floor opening-balance gap. (§4.5)
6. SoA `Ending Balance` on a T+2 buy: trade-date or due-date basis? (§4.10)
7. Any Trade Confirmation row where `Quantity ≠ Lot × 100`. (§5)

---

## Sources

- POJK No. 13 Tahun 2025 — <https://ojk.go.id/id/regulasi/Documents/Pages/POJK-13-Tahun-2025-Pengendalian-Internal-dan-Perilaku-Perusahaan-Efek-yang-Melakukan-Kegiatan-Usaha/POJK%2013%20Tahun%202025%20Pengendalian%20Internal%20dan%20Perilaku%20Perusahaan%20Efek%20yang%20Melakukan%20Kegiatan%20Usaha%20Sebagai%20Penjamin%20Emisi%20Efek%20dan%20Perantara%20Pedagang%20Efek.pdf>
- Peraturan Bapepam-LK No. V.D.3 (Kep-548/BL/2010) — <https://www.ojk.go.id/Files/regulasi/pasar-modal/bapepam-pm/pe-wpe-pi-rd/perusahaan-efek/V.D.3.pdf>
- KSEI, Fasilitas AKSes KSEI — <https://web.ksei.co.id/education/akses-facility>
- KSEI, Panduan Pengguna Web AKSes – Portofolioku (21 April 2020) — <https://web.ksei.co.id/files/Panduan_Pengguna_Web_AKSes_-_Portofolioku.pdf>
- Stockbit Terms — <https://stockbit.com/terms>
- Stockbit help, Tax Report / Coretax — <https://help.stockbit.com/id/article/bagaimana-cara-melaporkan-pajak-saham-di-spt-online-coretax-16odghf/>
- Stockbit Snips, E-Statement for SPT — <https://snips.stockbit.com/edukasi/lapor-spt-2022-lebih-mudah>
- Stockbit help, Portfolio Performance — <https://help.stockbit.com/id/article/performance-apa-itu-portfolio-performance-dan-bagaimana-cara-melihatnya-1m46xja/>
- Stockbit help, Total Equity Return — <https://help.stockbit.com/id/article/performance-bagaimana-cara-menghitung-total-equity-return-pada-portfolio-performance-1trirrg/>
- Stockbit help, transaction history — <https://help.stockbit.com/id/article/history-bagaimana-cara-melihat-riwayathistory-transaksi-1uwoxlm/>
- Stockbit help, Trading Balance vs RDN — <https://help.stockbit.com/id/article/transaksi-saham-apa-perbedaan-trading-balance-dan-rdn-1v37rji/>
- Stockbit help, multiple portfolio transfers — <https://help.stockbit.com/id/article/multiple-portfolio-bagaimana-cara-transfer-dana-transfer-saham-jika-memiliki-multiple-portfolio-lr4klk/>
- Stockbit help, odd lot — <https://help.stockbit.com/id/article/transaksi-saham-bagaimana-cara-belijual-dan-sembunyikan-saham-odd-lot-dari-portfolio-132yk1u/>
- IDX Peraturan Nomor II-A (Kep-00055/BEI/03-2023) — <https://www.idx.co.id/Media/y0vjxqur/signed_peraturan_ii_a_perdagangan_efek_bersifat_ekuitas.pdf> (403 to non-browser clients; rule identifier verified via IDX index listing)
