# IBKR Flex — sample findings

Empirical answers to the open items in `docs/research/ibkr-trade-export.md`.
Produced by `scripts/sample-exports-wizard.sh` on 2026-08-12.

The raw Flex statement is deliberately NOT in this repo.

### Does Flex offer a custom date range beyond the presets? (open item 5)

n

### 'Should Expire After' options (open item 2)

Offered: 1 day, 1 week

Chosen: 1 year, 2027-07-14, 10:43:17 EDT

Expiry surfaces as error 1012, which the daily job must escalate, not retry.
