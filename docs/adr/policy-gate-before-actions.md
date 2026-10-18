# Deterministic policy gate between the model and any action

**Context.** Language models can be wrong or manipulated, and sending a collections message is hard to undo.

**Decision.** The LangGraph workflow only proposes. Plain Python rules (promise breaks, high value, reminder caps, cooldown) decide, and anything held waits for a person.

**Consequences.** Thresholds are constants today; per-workspace configuration is planned.
