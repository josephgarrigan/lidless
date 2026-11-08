# lidless documentation

Start with [`../README.md`](../README.md) for what lidless is and how to deploy it.

## Decisions

- [ADR index](adr/README.md)
- [ADR-0001 — what may leave a host](adr/ADR-0001-decision-gate-payload-boundary.md) — the
  payload boundary for log-derived material, and the reasoning behind it
- [ADR-0002 — reconciliation is advisory](adr/ADR-0002-reconciliation-is-advisory.md) — why
  the decision gate does not hold the credential that changes the world

## Operating the collector

- [`roles/observability/EDGE.md`](../roles/observability/EDGE.md) — the collector as it
  actually runs: one process per node, what it reads, what it ships, and the failure
  modes worth knowing before you deploy it

## Configuration

- `rules/schema/tenant-policy.schema.json` — the policy shape the gate validates against
- `group_vars/all.yml.example` and `roles/observability/defaults/` — collector settings
