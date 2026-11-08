# lidless

A small, memory-bounded log collector with a decision gate in front of it.

lidless is two things that ship together:

1. **An edge collector.** One [Vector](https://vector.dev) process per host, capped at a few
   tens of megabytes of RAM, which forwards only what matters — errors, criticals, and
   the events an operator declared interesting — to a central store. Nothing is retained
   on the edge; the collector is a filter, not a warehouse.
2. **A decision gate.** A Python service that turns those events into a typed judgement:
   what class of fault this is, how severe, whether a human is required, and whether an
   automated remedy is permitted. The gate is **advisory by design** — it emits decisions
   and a reconciliation proposal, and a separate component with its own credential decides
   whether to act.

It is deliberately small. The interesting problem it solves is not "collect logs"; it is
"collect almost nothing, and be able to justify every event that left the host."

## What it is not

- Not a SIEM and not a log warehouse. There is no long-term edge retention and no query
  language of its own; the centre does that.
- Not an autoscaler or a remediation engine. The gate classifies; remediation is a
  separate, credentialed act.
- Not turnkey. You supply the inventory, the centre endpoint, and the policy for what
  your hosts may emit.

## How it fits together

```
   host                            host                            host
 ┌──────────────┐               ┌──────────────┐               ┌──────────────┐
 │ docker /     │               │ docker /     │               │ journald     │
 │ journald     │               │ journald     │               │              │
 └──────┬───────┘               └──────┬───────┘               └──────┬───────┘
        │                              │                              │
   ┌────▼─────┐                   ┌────▼─────┐                   ┌────▼─────┐
   │ Vector   │  qualified,       │ Vector   │                   │ Vector   │
   │ (edge)   │  bounded, no      │ (edge)   │                   │ (edge)   │
   │          │  disk buffer      │          │                   │          │
   └────┬─────┘                   └────┬─────┘                   └────┬─────┘
        └──────────────┬───────────────┴──────────────────────────────┘
                       ▼
              ┌──────────────────┐        ┌───────────────────────────┐
              │ central store    │───────▶│ decision gate             │
              │ (your choice)    │        │ classify → decide → route │
              └──────────────────┘        └─────────────┬─────────────┘
                                                         │ advisory
                                                         ▼
                                              ┌───────────────────────┐
                                              │ credentialed actor    │
                                              │ (separate identity)   │
                                              └───────────────────────┘
```

The gate never holds the credential that can change the world. That separation is the
point of the design, not an implementation detail — see
[`docs/adr/ADR-0002-reconciliation-is-advisory.md`](docs/adr/ADR-0002-reconciliation-is-advisory.md).

## Quickstart

Requirements: Debian 12 hosts with Docker, Ansible on the control machine, Python 3.11+
for the gate.

```bash
git clone <this repo> lidless && cd lidless
python3 -m pip install -r requirements-dev.txt

cp inventory.ini.example inventory.ini      # list your hosts
$EDITOR group_vars/all.yml                  # centre endpoint, tenant name, labels

ansible-playbook -i inventory.ini edge.yml  # deploy the collector only
```

`edge.yml` deploys the collector. `site.yml` deploys the full stack (collector, a
read-only log UI, and probe-based health checks) and is a larger footprint; start with
`edge.yml`.

Verify what you deployed:

```bash
python3 scripts/check_host_freshness.py \
  --centre http://your-centre:9428 \
  --hosts host-a,host-b \
  --window 6h
```

A quiet host and a broken collector look identical from the centre. That script exists
because "no events" is not the same as "nothing to report", and it exits non-zero when a
host has gone silent.

## Repository layout

```
roles/observability/     Ansible role: collector install, config templates, unit files
scripts/                 render_edge_config.py (config from inventory), freshness check
windmill/flows/          the decision gate: classification, cache, state, DLQ, backflow
rules/                   tenant policy schema + example policies
docs/adr/                the decisions that shaped the design, including what may leave a host
tests/                   offline suites; no network, no credentials
```

## What the gate decides

For each event it produces a judgement with a fixed shape: `fault_class`, `severity`,
`user_impact`, `recommendation`, `action_required`, and a `reason_code` when it declines
to act. Classification is **ordered and explicit** rather than emergent — the first
matching rule wins, and the order is reviewable.

Two properties are enforced rather than promised:

- **Fail closed on unparseable input.** An event the gate cannot classify is routed to a
  dead-letter path with its reason, not silently dropped and not guessed at.
- **A decision is reproducible.** Given the same event and policy, the gate produces the
  same judgement; anything that varies (clock, cache, upstream availability) is recorded
  in the judgement rather than hidden.

## Configuration

Collector settings live in `group_vars/all.yml` and `roles/observability/defaults/`.
The ones that matter:

- centre endpoint and protocol
- tenant label attached to every event
- which sources to read (`docker`, `journald`, both)
- the level threshold below which nothing is shipped

Policy for the gate lives in `rules/tenants/*.yaml` and is validated against
`rules/schema/tenant-policy.schema.json` before use; a policy that does not validate is a
startup error, not a warning.

## Testing

```bash
for t in tests/test_*.py; do python3 "$t"; done
```

The suites are offline by design: no network, no credentials, no live estate. The two
harnesses that need a live service (`tests/bench_latency.py`, `tests/jev_accuracy.py`)
are deliberately outside that glob.

## Status and scope

This is an extracted working system, published so the design can be read and reused. It
is not a supported distribution: interfaces may change, and the examples are examples.
See `docs/adr/` for which constraints are load-bearing and why.

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, ship it; the only obligation is keeping the
copyright notice with substantial copies.
