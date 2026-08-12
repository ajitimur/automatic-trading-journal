# The journal records executed trades, not plans

There is no Plan entity. The journal never captures pre-trade intent — no planned entry, no planned size — because the trader will not reliably record intent before a trade, and a field that goes unfilled is worse than a field that does not exist. The Trade holds what was *executed*, derived from fills, plus exactly two hand-entered values: the **stop** and the **setup**.

Those two earn their keystrokes because nothing else can supply them. The stop is genuinely discretionary — it follows no rule derivable from the daily bars — and without it there is no risk percentage and no realized R. The setup is the only input the setup-selection learning goal has.

## Consequences

**Sizing analysis is descriptive, not adherence-based.** With no recorded intent, the journal can answer "how large were the bets, and did size track outcome" but not "did the trader size the bet as they intended." The latter question is structurally unavailable and should not be reintroduced by adding a planned-size field.

Rule adherence narrows to what daily bars can verify on their own — principally whether exits followed the mechanical rule — plus the stop as executed. This is a deliberate contraction of the original scope, which assumed a planned stop.

Because the stop is typed and backdated manual entry is first-class, a stop may be reconstructed from memory after the outcome is known. **Stop provenance** (`recorded` vs `reconstructed`) is therefore derived from whether the stop was entered before the Trade's first exit, so hindsight-contaminated stops can be excluded from discipline scoring without the trader self-reporting confidence.
