# Stops are declared at entry through a Telegram relay

The trader declares each Stop from the phone, as a Telegram message sent at the moment the stop-loss is placed at the broker, before the journal has seen the Fills it belongs to. The same chat carries the Stockbit TC PDF, forwarded by hand in the evening. A thin relay, the one component that runs off this machine, receives the bot's webhook and holds messages until the daily run pulls them. The journal, its database, the review surface and the Flex token stay local. This amends SPEC §13's *"no cloud"* and replaces §13.2's hand-drop with a forward, while leaving its refusal of mailbox access intact.

## Why the stop has to come from the trader

[ADR 0010](0010-confirm-demands-the-stop.md) made confirm demand an answer, but the demand still arrived at the laptop, hours after the trade. The IDX TC lands between roughly 17:50 and 19:35 WIB, when the trader is away from the laptop or winding down, and about 60% of trading on both books happens from the phone. The trader always places a hard stop-loss at the broker, so the Stop exists at entry. The journal was losing it because it only asked later.

Neither broker can supply it. IBKR Flex reports executions only: a stopped-out exit carries `orderType=STP` and its fill price, never the trigger, and working orders never appear. Reading a live order needs a TWS or Gateway session with weekly re-authentication. Stockbit sends an auto-order to the exchange as a plain order, and no TC, SoA or email carries the trigger price. So the trader has to state it, and the cheapest moment is the one where they have just typed it into the broker.

## Why Telegram, and why a relay

**iCloud Drive via an iOS Shortcut** was the strongest local alternative: durable, no token, no third party beyond Apple. It was rejected because it is one-way. The journal could not acknowledge a declaration, and a Stop is immutable once bound, so a typo caught on the spot is worth more than it looks. The trader also could not answer a nag in the same place it arrived.

**Telegram with no relay** fails on durability: the Bot API discards undelivered updates after 24 hours. A laptop asleep over a weekend would silently lose stops, and a record that can lose itself is worse than none.

**Deploying the whole journal** fixes durability, and also sidesteps the ISP DNS interception. But it needs a public authenticated surface for the workbench, moves the Flex token and the keep-forever raw archive onto a server, and buys uptime that backfill (§13.1) had already made unnecessary. The 3-trading-day grace window ([ADR 0009](0009-stops-are-never-backfilled.md)) means a next-morning nag still lands in time.

The relay keeps the one property Telegram lacks, durability, without moving anything that matters. It holds no broker credential and does no binding, gating or committing.

## What the relay does, and nothing more

- Accepts messages only from the trader's Telegram user ID; anything else is dropped silently.
- Parses the grammar (`SYMBOL STOP [SETUP]`, `SYMBOL no sl`, an optional `idx`/`us` prefix) and replies at once with how it read the message: Book, stop, setup, and the entry date taken from Telegram's timestamp in the Book's exchange time zone.
- Stores messages and forwarded PDFs until the Mac acknowledges pulling them, then deletes them. A 30-day TTL bounds it if the Mac never does, so the relay never becomes a second copy of the trading history.
- On a day with IDX declarations and no forwarded PDF, reminds the trader at 20:00 WIB to forward the TC. This is the only logic it owns, because it is the only nag that must fire while the Mac sleeps.

## Why the TC is forwarded rather than fetched

A Google Apps Script polling Gmail would make IDX intake need no action from the trader, at the cost of a script with Gmail read scope in the trader's account. The trader chose the forward instead, keeping §13.2's line on mailbox access. What made that acceptable is the declaration itself: with stops declared at entry, a missing TC is no longer invisible, because the journal knows a trade happened before the PDF arrives. A trade with neither a declaration nor a forwarded TC still surfaces only at the monthly SoA, as before.

## Consequences

**Declarations and forwarded PDFs are source documents.** The Mac files them in the keep-forever raw archive alongside Flex XML and TCs, so the database stays reconstructible from what was actually said and when.

**The Mac now talks to one more host, the relay**, through the DoH transport built for Flex. Outbound nags go through the relay too (`POST /send`, #53) rather than straight to `api.telegram.org`, so the bot token stays on the Worker and the Mac holds only its relay secret.

**The relay remembers the last week's declarations, beyond the acknowledgement.** A resend has to say what it replaced, and `/undo` has to know what it withdraws, even after the Mac has pulled the earlier message. So the relay keeps the latest declaration per (book, symbol, entry date) for 7 days, apart from the items it deletes on acknowledgement. Seven days covers the 3-trading-day lapse window across a weekend or a holiday, after which no correction can matter. This is a short memory, not a second copy of the history. The relay still cannot know what has bound, so a replace or `/undo` that arrives after binding is answered on the Mac's side.

**Book inference can misfire on 4-letter US tickers.** A 4-letter symbol reads as IDX unless the journal has seen it on the US book, which means the Mac supplies the relay a small list of known US symbols. The acknowledgement always echoes the Book, so a wrong guess is visible before it binds.
