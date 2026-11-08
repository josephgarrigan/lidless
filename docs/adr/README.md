# Architecture Decision Records

Append-only. One file per consequential decision. Status is one of `proposed`, `accepted`, `superseded`, `rejected`. Never edit a decision after acceptance — supersede it with a new ADR that names the old one.

| ID | Title | Status | Date |
| --- | --- | --- | --- |
| [ADR-0001](ADR-0001-decision-gate-payload-boundary.md) | Decision-gate payload boundary | accepted (pending operator review) | 2026-09-17 |
| [ADR-0002](ADR-0002-reconciliation-is-advisory.md) | Policy reconciliation by a model is advisory, never authoritative | accepted (pending operator review) | 2026-09-17 |

## What belongs here

Decisions that are expensive to reverse, or that constrain future work in a way a reader six months from now would not infer from the code. For this repository that has meant: what may leave the fleet, and what authority an external participant may hold. Both ADR-0001 and ADR-0002 exist because the answers were non-obvious and the failure modes were silent.

## What does not

Question wording, threshold values, tenant configuration, and anything reversible by editing one file under `rules/`. Those live in `rules/tenants/*.yaml` and their calibration reports.
