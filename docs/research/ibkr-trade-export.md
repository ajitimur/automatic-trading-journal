# How trades get out of IBKR

Research for [#4](https://github.com/ajitimur/automatic-trading-journal/issues/4) (part of #1).
All pages accessed **2026-08-12**. Sources are IBKR-owned only: `ibkrguides.com` (the official
Reporting/Portal guides), `interactivebrokers.github.io` (the official TWS API reference), and
`interactivebrokers.com/docs` + `/campus` (IBKR Campus / Web API docs).

> Fetch note: `interactivebrokers.com` was not directly resolvable from the research machine (ISP
> DNS interception). Those pages were read through a text-extraction proxy that fetches the same
> origin. Content is IBKR's; if a quote below matters for a build decision, re-read the URL from a
> clean network before relying on it.

---

## 1. Verdict

**Primary route: Flex Web Service, running a saved *Activity Flex Query* with the Trades section at
`Level of Detail = Executions`.**
It is the only route of the three that is genuinely unattended: a token in a URL, two plain HTTP
GETs, no browser, no 2FA prompt, no desktop process.

**Fallback route: scheduled delivery of the same Flex Query** (daily email, or sFTP on request) with
the daily job ingesting the dropped file instead of pulling it. Same report, same fields, no token
to expire — the failure mode moves from "token died" to "file didn't arrive".

**Rejected for the daily job:**

- **Client Portal Web API** — for an individual (non-advisor, non-broker) account the only
  authentication method is the Client Portal Gateway, which requires a human to open a browser and
  log in with 2FA. Disqualifying.
- **TWS API** — requires a running, interactively logged-in TWS or IB Gateway, and only returns the
  current day's executions. Disqualifying twice over.

This also fits the map's `confirm-and-enrich` intake decision (#1): a Flex file is a statement drop,
which is exactly the intake shape already chosen.

---

## 2. Route comparison

| | Flex Web Service | Scheduled Flex delivery | Client Portal Web API | TWS API |
|---|---|---|---|---|
| Setup | Build query in portal, enable service, copy token | Build query, enable delivery | Download/run Java gateway, or apply for OAuth | Install TWS/IB Gateway, enable API |
| Auth | Token in query string | None (push) | Browser SSO + 2FA (individual accounts) | Interactive login window |
| Unattended? | **Yes** | **Yes** | **No** | **No** |
| History depth | Up to 365 calendar days per query period | Same | **7 days max** | **Current day** (since midnight) |
| Execution-level detail | Yes (`Level of Detail = Executions`) | Yes | Yes (one row per execution) | Yes (one callback per fill) |
| Ongoing babysitting | Rotate token before expiry; handle "not ready" retries | Watch for missing file | Re-login constantly | Keep a desktop app alive; weekly credential re-entry |

---

## 3. Flex Web Service — the recommended route

### 3.1 Protocol

Two-step, version 3
([Flex Web Service Version 3](https://www.ibkrguides.com/complianceportal/complianceportal/flexweb3.htm)):

1. `https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest?t=TOKEN&q=QUERY&v=3`
   → returns `Status=Success`, a `ReferenceCode`, and a `Url`.
2. `https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement?t=TOKEN&q=REF_CODE&v=3`
   → returns the report.

Parameters are only `t` (token), `q` (query id, then reference code), `v` (version; **defaults to 2
if omitted**, so always send `v=3`). Failures return `Status=Fail` with `ErrorCode` and
`ErrorMessage`.

**There is no date parameter.** The reporting period is baked into the saved query. Consequence for
the build: the daily job cannot ask for "trades since X" — it asks for whatever window the saved
query defines, and a historical backfill needs a *second saved query* (or a manual edit in the
portal). Design the importer around a fixed overlapping window plus dedupe, not around a cursor.

### 3.2 Rate limits and error codes

Rate limit is stated only inside the error table: **"Limited to one request per second, 10 requests
per minute (per token)"** (error 1018)
([Flex Web Service Version 3 error codes](https://www.ibkrguides.com/clientportal/flex3.htm)).

Codes the daily job must handle rather than treat as fatal — these are *normal*, not bugs:

| Code | Message | Meaning for the job |
|---|---|---|
| 1001 | "Statement could not be generated at this time. Please try again shortly." | Retry with backoff |
| 1004 | "Statement is incomplete at this time. Please try again shortly." | Retry — data still landing |
| 1005 / 1006 / 1007 / 1008 | Settlement / FIFO P/L / MTM P/L "data is not ready at this time" | Retry; or don't request P/L fields |
| 1009 | "The server is under heavy load." | Retry |
| 1019 | "Statement generation in progress. Please try again shortly." | Expected between SendRequest and GetStatement |
| 1021 | "Statement could not be retrieved at this time." | Retry |
| **1012** | **"Token has expired."** | **Hard stop — needs a human. Alert loudly.** |
| **1015** | **"Token is invalid."** | **Hard stop — token was regenerated elsewhere** |
| 1013 | "IP restriction." | Job moved hosts; token was IP-pinned |
| 1018 | Too many requests | Back off |

Full list at the same URL. Note 1019 is the designed happy path: `SendRequest` returns a reference
code before the report exists, so `GetStatement` must poll.

### 3.3 Timing

Daily statement data is not instant. **"The statement cutoff time for commodities is generally 5:15
PM EST, and the statement cutoff time for securities is generally 8:20 PM EST."**
([Statements](https://www.ibkrguides.com/clientportal/performanceandstatements/statements.htm))
Schedule the daily job well after the securities cutoff, and treat 1004/1005 as "come back later".

---

## 4. The field list

From the **Trades** section of an Activity Flex Query
([Trades — Flex Statement](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm)).
Complete field list as published, grouped for readability — every name below is verbatim from that
page:

**Identity / dedupe**
`Trade ID`, `IB Order ID`, `IB Execution ID`, `Brokerage Order ID`, `Exchange Order ID`,
`External Execution ID`, `Order Reference`, `Level of Detail`

**Timestamps**
`Report Date`, `Trade Date`, `Trade Time`, `Order Time`, `Order Placement Time`, `Open Date Time`,
`Settle Date Target`

**The trade itself**
`Buy/Sell`, `Transaction Type`, `Quantity`, `Trade Price`, `Multiplier`, `Trade Money`, `Proceeds`,
`Net Cash`, `Close Price`, `Open/Close Indicator`, `Order Type`, `Is API Order`, `Exchange`,
`Notes/Codes`

**Money**
`Currency`, `FX Rate to Base`, `IB Commission`, `IB Commission Currency`, `Taxes`, `Cost Basis`,
`Realized PNL`, `MTM PNL`

**Instrument**
`Account ID`, `Account Alias`, `Model`, `Asset Class`, `Symbol`, `Description`, `Conid`,
`ListingExchange`, `Security ID`, `Security ID Type`, `CUSIP`, `ISIN`, `FIGI`, `Issuer`,
`UnderlyingConID`, `Underlying Conid`, `Underlying Symbol`, `UnderlyingListingExchange`,
`Strike`, `Expiry`, `Put/Call`, `Principal Adjustment Factor`

**Corrections / wash sales / other**
`Original Trade Price`, `Original Trade Date`, `Original Trade ID`, `Original Order ID`,
`Change in Price`, `Change in Quantity`, `Holding Period Date Time (Wash Sales)`,
`When Realized (Wash Sales)`, `When Reopened (Wash Sales)`, `Clearing Firm ID`, `Trader ID`,
`Volatility Order Link`, `Commodity`, `Delivery`, `Weight`, `Fineness`, `Serial Number`

Every field the ticket asked for is present:

| Ticket asks for | Flex field |
|---|---|
| execution id | `IB Execution ID` (plus `Trade ID`, `External Execution ID`) |
| order id | `IB Order ID` (plus `Brokerage Order ID`, `Exchange Order ID`, `Order Reference`) |
| timestamps | `Trade Date` + `Trade Time`, `Order Time`, `Order Placement Time`, `Report Date` |
| side | `Buy/Sell` (also `Transaction Type`) |
| quantity | `Quantity` |
| price | `Trade Price` |
| commission | `IB Commission` + `IB Commission Currency` (and `Taxes` separately) |
| currency | `Currency` (+ `FX Rate to Base`) |

**Gotcha: four of these are gated behind a checkbox.** `Brokerage Order ID`, `Order Reference`,
`Volatility Order Link`, and `Order Placement Time` only appear if **"Include Audit Trail fields"**
is enabled in the query's General Configuration
([Delivery Configuration and General Configuration](https://www.ibkrguides.com/reportingreference/reportguide/delivery%20configuration%20and%20general%20configuration.htm)).

### 4.1 Timezone — UNVERIFIED

**No IBKR page consulted states the timezone of `Trade Time`, `Order Time`, or `Open Date Time`.**
The Trades field reference gives only "Trade Time: The trade time." and equivalents; the default
Activity Statement Trades page
([Trades — Default Activity Statement](https://www.ibkrguides.com/reportingreference/reportguide/trades_default.htm))
says nothing either. The General Configuration section exposes a `Date Format`, `Time Format` and
`Date/Time Separator` but the docs do not enumerate the options or name a zone.

The only timezone IBKR states anywhere nearby is EST, for statement cutoffs (§3.3). That is
suggestive, not evidence.

**Do not guess this.** Action for the build: pull one real Flex file containing a trade whose
execution time is independently known and pin the zone empirically, then record it in `CONTEXT.md`.
Until then treat trade timestamps as zone-unknown and do not derive holding periods across a DST
boundary from them.

---

## 5. Partial fills

This is the part the map explicitly worried about ("one mis-parsed partial fill poisons the exit
analysis"), and the answer is good but has a sharp edge.

**Representation is a configuration choice.** The Trades section takes a *Level of Detail*, and the
choices are **"Symbol Summary, Executions, Orders, Asset Class, Closed Lots, Wash Sales"**
([Trades — Flex Statement](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm)).
The emitted `LevelofDetail` field is described as **"Executions, orders or closed lots"**
([Trade Confirmation Configuration](https://www.ibkrguides.com/reportingreference/reportguide/trade%20confirmation%20configuration.htm)).

- `Executions` → **one row per fill**.
- `Orders` → rows aggregated to the order.
- `Symbol Summary` → aggregated further, to the symbol.

You may select more than one level in a single query ("select one or more Levels of Detail"), which
means a single file can contain the *same* trade at several granularities. **Filter on the
`Level of Detail` field when importing**, or the journal will double-count. Include the field in the
query output for exactly this reason.

**IBKR treats each fill as its own trade.** "In the case of partial executions, each execution is
considered one trade."
([Commissions — Stocks](https://www.interactivebrokers.com/en/pricing/commissions-stocks.php))
The TWS API reference states the same at the identifier level: **"Each partial fill has a separate
ExecId."**
([Execution Class Reference](https://interactivebrokers.github.io/tws-api/classIBApi_1_1Execution.html))

### 5.1 The sharp edge: commission is not spread across fills

> "In case of partial executions, commissions are charged on the total quantity executed on the
> original order. The commission is displayed on the first partial execution only."
> — [Notes/Legal Notes — Trade Allocations](https://www.ibkrguides.com/reportingreference/reportguide/noteslegal%20notes%20trade%20allocations.htm)

So at `Level of Detail = Executions`, a 3-fill order yields three rows where **row 1 carries the
whole order's commission and rows 2 and 3 carry zero**. Any per-fill "net price after cost" the
journal computes will be wrong on every row.

Direct consequence for the trade-record shape (feeds #6): **commission must be modelled at the order
level, not the fill level.** Group fills by `IB Order ID`, sum quantity, compute a
quantity-weighted average price, and attach the order's single commission to that group. Do not
store a per-fill commission as if it were meaningful.

---

## 6. Incremental vs. full, and dedupe

### 6.1 The export is a full window, never a delta

Flex Query period options are:
**"Last Business Day, Last Business Week, Last Month, Last Quarter, Last 30 Calendar Days,
Last 365 Calendar Days, Last N Calendar Days, Month to Date, Quarter to Date or Year to Date"**
([Create an Activity Flex Query](https://www.ibkrguides.com/orgportal/performanceandstatements/activityflex.htm);
same list for [Trade Confirmation Flex Queries](https://www.ibkrguides.com/clientportal/performanceandstatements/tradeflex.htm)).

There is no "since last run" option and, per §3.1, no date parameter on the wire. Every run returns
the full window. **The importer must be idempotent by construction.**

Recommended shape: a saved query with `Last N Calendar Days`, N ≈ 5–7. The overlap absorbs weekends,
holidays, a job that failed for two days, and late-settling corrections, and dedupe throws away the
repeats. A one-off `Last 365 Calendar Days` query covers backfill. Max reachable history via a
period option is 365 calendar days — deeper history needs a custom date range configured in the
portal (**unverified**: the pages consulted list the period presets but do not confirm an arbitrary
custom-date-range option is offered for Flex specifically).

### 6.2 Dedupe key

Use **`IB Execution ID`** as the natural key, with `Trade ID` as a secondary. Both are per-fill at
`Level of Detail = Executions`.

Two documented facts shape the dedupe logic, both from the TWS API reference for the same
underlying execution identifier:

- **"Each partial fill has a separate ExecId."**
- Corrections arrive as a repeat of the execution with **"all parameters identical except for the
  execID in the Execution object. The execID will differ only in the digits after the final period."**
  ([Executions and Commissions](https://interactivebrokers.github.io/tws-api/executions_commissions.html))

So an exec id looks like `<base>.<seq>`, and a *correction* is the same `<base>` with a bumped `<seq>`.
That gives the journal a two-level key:

- **Logical execution** = exec id up to the last `.`
- **Version** = the digits after it; highest version wins, earlier versions are superseded

Treating the full string as the key would silently store both the wrong and the corrected fill.

**UNVERIFIED (important):** the "digits after the final period" semantics is documented for the *TWS
API* `Execution.ExecId`. No IBKR page consulted states that Flex's `IB Execution ID` is the same
identifier with the same format. It is very likely the same value — the Flex field is literally named
"IB Execution ID" — but confirm against a real file before hard-coding the split-on-last-dot rule.
Flex separately carries `Original Trade ID` / `Original Order ID` / `Original Trade Price` /
`Change in Price` / `Change in Quantity`, which look like the Flex-native way corrections are
expressed; whichever mechanism actually appears in a real file is the one to build against.

**Also unverified:** that `Trade ID` and `IB Execution ID` are *stable across reruns of the same
query*. No IBKR page consulted makes that guarantee explicitly. The correction mechanics above imply
stability (a changed id signals a correction, not a rerun), but this deserves an empirical check —
run the same query twice on consecutive days over an overlapping window and diff the ids. This is
cheap to test and expensive to get wrong.

---

## 7. Auth model and what will silently break

### 7.1 Flex Web Service token

Enabled at **Performance & Reports → Flex Queries → Flex Web Service Configuration**
([Enable Flex Web Service](https://www.ibkrguides.com/clientportal/performanceandstatements/flex-web-service.htm)).

From [Configure Flex Web Service](https://www.ibkrguides.com/brokerportal/performanceandstatements/flex3.htm):

- **"The token is valid for a 6 hour period by default."**
- **"In the Should Expire After list, select the amount of time before the token expires."**
- **"In the Valid For IP Address field, enter an IP address to restrict the token to that address."**
  — "If you leave this field blank, there will be no IP address restrictions."
- **"Note that when you generate a new token, you invalidate the current one."**

**The 6-hour default is a trap for a daily job.** A token created with defaults dies before the
second run. The "Should Expire After" dropdown must be set to a long duration at creation time.

**UNVERIFIED:** the durations offered in that dropdown. No IBKR page consulted enumerates them. Read
the dropdown when provisioning and write the chosen expiry, and the resulting expiry *date*, into
the runbook — then set a calendar reminder ahead of it. Expiry surfaces as error `1012 "Token has
expired."`, which the job must escalate rather than retry.

Second silent-breakage path: **regenerating a token invalidates the previous one.** If a human ever
clicks "Generate A New Token" for an unrelated reason, the job starts failing with `1015`. Third:
`1013 "IP restriction."` if the token was IP-pinned and the job's host or egress IP moves — a real
risk given hosting for the daily job is still undecided in #1.

**No 2FA, no interactive login, no session.** That is the whole reason this route wins.

Forward-looking: IBKR states it is consolidating "the Client Portal Web API, Digital Account
Management, and Flex Web Service under one interface with OAuth 2.0" authentication
([Web API Documentation](https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/)).
Existing endpoints "remain supported", but this is the thing most likely to change under the
project's feet. Isolate the Flex client behind one seam.

### 7.2 Client Portal Web API — why it is out

Three authentication methods
([Web API Authentication — Introduction](https://www.interactivebrokers.com/docs/web-api/authentication/introduction)):

1. **Client Portal Gateway** — "a Java client that reverse-proxies authentication through SSO."
   Supported accounts: **Individual Accounts only.**
2. **OAuth 1.0a** — "fully programmatically", but supported accounts are Advisor, Broker & FCM,
   Proprietary Trading Group, Hedge and Mutual Fund, Institutional Hedge Fund Investors, and Third
   Party Software Developers.
3. **OAuth 2.0** — same supported-account list.

**A plain individual retail account gets the Gateway and only the Gateway.** The programmatic options
are for account types this project is not.

And the Gateway requires a human. Per
[Launching and Authenticating the Gateway](https://www.interactivebrokers.com/campus/trading-lessons/launching-and-authenticating-the-gateway/):
run `bin/run.sh root/conf.yaml` and "keep this window open", then "open a browser and navigate to
https://localhost:5000 ... then login with your Live or Paper Account credentials". And:
**"IBKR requires all users to be two-factor authenticated and does not allow users to partially or
fully opt out."**

Further disqualifiers even if auth were solved:

- The trade endpoint is `GET /v1/api/iserver/account/trades`, whose `days` parameter is "The number
  of prior days prior to include in response, **up to a maximum of 7**. If omitted, only the current
  day's executions will be returned."
  ([Trade History](https://www.interactivebrokers.com/docs/web-api/api-reference/trading/trading-orders/get-trade-history))
  A 7-day ceiling means any outage longer than a week loses data permanently.
- Sessions need continuous `/tickle` keep-alive, and "brokerage functions reset nightly at ~01:00
  regional time"; also "a single username can only have one brokerage session active at a time
  across all IB platforms" — so the journal's job would fight the human's own TWS/mobile login.
  ([Web API Documentation](https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/))

For completeness, the response fields are execution-level and include `execution_id`, `order_id`,
`order_ref`, `side` (S/B), `size`, `price`, `commission`, `net_amount`, `trade_time`, `trade_time_r`
(ms), `exchange`, `conid`, `account`. **Note there is no explicit currency field in the documented
schema** — `net_amount` and `commission` are unlabelled numbers. Another mark against it for a
journal that must stay native-currency per book.

### 7.3 TWS API — why it is out

- **"For an application to connect to the API there must first be a running instance of TWS or IB
  Gateway."** Both require manual authentication via a login window; headless operation is not
  supported. Version 974+ has an autorestart feature allowing Sunday-to-Saturday operation, but
  **credentials must be re-entered after the Saturday night server reset.**
  ([Initial Setup](https://interactivebrokers.github.io/tws-api/initial_setup.html))
  A weekly manual credential re-entry is precisely the "silently breaks the daily job" failure the
  ticket asks about.
- `reqExecutions` returns "only those executions occurring since midnight for that particular
  account." Extending to 7 days requires changing TWS's Trade Log "Show trades for..." setting — and
  **"IB Gateway cannot modify these settings and remains limited to midnight-forward data."**
  ([Executions and Commissions](https://interactivebrokers.github.io/tws-api/executions_commissions.html))
  So the headless-*er* of the two IBKR desktop apps is capped at one day.
- Default `Read Only` API mode blocks order information; it must be turned off manually.

Its data model is the best of the three (`Execution` carries `ExecId`, `OrderId`, `PermId`, `Time`,
`Side` as BOT/SLD, `Shares`, `Price`, `CumQty`, `AvgPrice`, `OrderRef`, `AcctNumber`, `Exchange`,
`LastLiquidity`, with a matching `CommissionReport`) — but it is unreachable without a human, so the
data model is moot. Note the same page also says the `Time` field is "the execution's server time"
with **no format or timezone documented** — the same gap as Flex.

### 7.4 Fallback: scheduled delivery

The same saved Flex Query can be pushed instead of pulled. On the Delivered Statements screen:
**"sFTP is available by request only; if you do not request sFTP delivery (contact
reportingintegration@interactivebrokers.com), Email is the only delivery method you can choose."**
Encryption is likewise "available by request only and applies to both email and sFTP delivery."
Daily and monthly Activity Statements, Activity Flex Queries, and Trade Confirmation Flex Queries
can all be configured this way.
([Statements Delivery](https://www.ibkrguides.com/clientportal/usersettings/deliveredstatements.htm))

Why it is a genuine fallback and not just a variant: **there is no token to expire.** It survives the
one failure mode most likely to kill the primary route. Its own weakness is the mirror image — a
silent non-delivery looks identical to "no trades today", so the ingester needs a freshness check
(expect a file every business day; alert on absence) rather than trusting silence.

Output format for either route: **"XML, CSV, Text (Pipe) or Text (Tab)"**
([Create an Activity Flex Query](https://www.ibkrguides.com/orgportal/performanceandstatements/activityflex.htm)).
Prefer **XML** — it is self-describing, and the Flex XML carries the section/attribute names rather
than relying on column order, which matters when a field gets added to the query later.

---

## 8. What feeds the spec

- **Trade-record shape (#6):** the record is fill-level for price/quantity but **order-level for
  commission**. `IB Order ID` is the grouping key; `IB Execution ID` is the row key.
- **Capture flow:** pull a fixed overlapping window daily after the 8:20 PM EST securities cutoff,
  retry on the "not ready" error family, dedupe on exec id, escalate on 1012/1015/1013.
- **Restatement** (listed as unspecified in #1): IBKR already has a correction mechanism —
  `Original Trade ID`, `Change in Price`, `Change in Quantity`, and exec-id version bumps. The
  journal's restatement design should absorb these, not just external data-source changes.

## 9. Open items — explicitly not established

| # | Unknown | How to settle it |
|---|---|---|
| 1 | Timezone of `Trade Time` / `Order Time` | Empirical: one known trade, one Flex file |
| 2 | Durations offered in "Should Expire After" | Read the dropdown at provisioning; record in runbook |
| 3 | Whether Flex `IB Execution ID` shares the TWS `ExecId` `<base>.<seq>` format | Inspect a real file |
| 4 | Whether ids are byte-stable across reruns of the same query | Run the same query two days running, diff |
| 5 | Whether Flex offers an arbitrary custom date range beyond the listed presets | Check the portal query builder |
| 6 | Whether Trade Confirmation Flex data lands earlier in the day than Activity Flex | Not documented on any page consulted; test if same-day capture is ever wanted |

---

## Sources

All accessed 2026-08-12.

**IBKR Reporting & Portal guides (ibkrguides.com)**
- Trades — Flex Statement: https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm
- Trades — Default Activity Statement: https://www.ibkrguides.com/reportingreference/reportguide/trades_default.htm
- Trade Confirmation Configuration: https://www.ibkrguides.com/reportingreference/reportguide/trade%20confirmation%20configuration.htm
- Delivery Configuration and General Configuration: https://www.ibkrguides.com/reportingreference/reportguide/delivery%20configuration%20and%20general%20configuration.htm
- Notes/Legal Notes — Trade Allocations: https://www.ibkrguides.com/reportingreference/reportguide/noteslegal%20notes%20trade%20allocations.htm
- Activity Flex Query Reference: https://www.ibkrguides.com/reportingreference/reportguide/activity%20flex%20query%20reference.htm
- Create an Activity Flex Query: https://www.ibkrguides.com/orgportal/performanceandstatements/activityflex.htm
- Trade Confirmation Flex Queries: https://www.ibkrguides.com/clientportal/performanceandstatements/tradeflex.htm
- Flex Web Service Version 3 (endpoints): https://www.ibkrguides.com/complianceportal/complianceportal/flexweb3.htm
- Flex Web Service Version 3 (error codes, rate limit): https://www.ibkrguides.com/clientportal/flex3.htm
- Configure Flex Web Service (token validity): https://www.ibkrguides.com/brokerportal/performanceandstatements/flex3.htm
- Enable Flex Web Service: https://www.ibkrguides.com/clientportal/performanceandstatements/flex-web-service.htm
- Statements (cutoff times): https://www.ibkrguides.com/clientportal/performanceandstatements/statements.htm
- Statements Delivery: https://www.ibkrguides.com/clientportal/usersettings/deliveredstatements.htm

**TWS API reference (interactivebrokers.github.io)**
- Initial Setup: https://interactivebrokers.github.io/tws-api/initial_setup.html
- Executions and Commissions: https://interactivebrokers.github.io/tws-api/executions_commissions.html
- Execution Class Reference: https://interactivebrokers.github.io/tws-api/classIBApi_1_1Execution.html

**IBKR Campus / Web API docs (interactivebrokers.com)**
- Web API Authentication — Introduction: https://www.interactivebrokers.com/docs/web-api/authentication/introduction
- Trade History endpoint: https://www.interactivebrokers.com/docs/web-api/api-reference/trading/trading-orders/get-trade-history
- Web API Documentation (sessions, rate limits, consolidation): https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/
- Launching and Authenticating the Gateway: https://www.interactivebrokers.com/campus/trading-lessons/launching-and-authenticating-the-gateway/
- Commissions — Stocks (partial executions): https://www.interactivebrokers.com/en/pricing/commissions-stocks.php
