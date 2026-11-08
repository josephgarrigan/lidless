# ADR-0001 — Decision-gate payload boundary

**Status:** **accepted** — adjudicated by the operator 2026-09-17 (Forgejo issue #1, closed) · **Date:** 2026-09-17 · **Deciders:** operator (operator), agent
**Supersedes:** — · **Related:** PLAN.md §2.5, OD-1

## Context

lidless is sold as an **ephemeral zero-retention** observability triad. Vector holds log data in an in-memory bus, `sanitize_message()` strips internal IPs, credentials, and path prefixes before any tenant-facing text is produced, and the README's memory budget exists precisely so nothing is buffered on disk.

Adding a decision gate introduces a new outbound path: log-derived state leaving the fleet for `api.typesafe.ai`, a US-hosted, early-access inference vendor with no published data-processing agreement, no published retention policy, and self-described unproven pricing ("we can't prove it isn't subsidized").

Two facts bound the decision:

1. **Ordering is currently unsafe for this purpose.** `sanitize_message()` is defined inside `synthesize_anecdote()` and runs *after* pattern matching. Wiring the gate into the existing call order would transmit raw `raw_message` values.
2. **The sanitized preview already leaves the perimeter.** It is the exact string lidless posts to Slack, Matrix, and PagerDuty. Sending that same redacted string to the gate adds a new *recipient*, not a new *class* of material.

Meanwhile, the gate cannot do its job without *some* signal about the error. Given structured fields alone (`status_code: 500`, `stream: stderr`, `container: payment_gateway`), it can resolve severity and transience but cannot distinguish an OOM kill from database connection exhaustion — precisely the distinction that decides whether a restart is appropriate.

## Decision

The gate supports two per-tenant state modes, with the mode declared in `rules/tenants/<tenant>.yaml` and enforced by `derive_state()`:

- **`sanitized_content`** — the default. State carries structured metadata plus **only** `sanitize_message(raw_message)`. Sanitization runs strictly before state construction, and only for tenants that have explicitly opted in.
- **`structured_only`** — no log text at all. Status code, stream, error flag, container, hostname, tenant, tier, timestamp, and counts. Fault-class granularity is lost; severity and transience judgments remain available.

Binding rules:

1. **Sanitize before egress, always.** `sanitize_message()` moves to `decision_state.py` as the single source of truth (L-003) and is called before any state is built. No code path may construct gate state from `raw_message`.
2. **`derive_state()` takes an explicit mode argument.** No default. A caller cannot accidentally select the wider mode.
3. **No raw payloads, ever** — not in the request, not in the audit envelope, not in the shadow ledger, not in a DLQ record, not in a Matrix notice. Tests assert this by scanning serialized state for every token of the input message.
4. **`structured_only` is available to any tenant on request**, and is the mode to use if a tenant contract forbids third-party inference. Loss is limited to `fault_class` granularity.
5. **Revisit trigger:** if TypeSafe offers a fleet-hosted or Nebula-only deployment, or publishes a DPA, this ADR is reopened and `sanitized_content` may become unnecessary.

## Consequences

**Positive.** The gate ships with the smallest defensible egress surface. Fault classification stays available for tenants who accept a US inference vendor on already-redacted text. The ordering fix (sanitize first) is a genuine improvement independent of Jev — today, ordering is an accident of function layout.

**Negative.** The audit trail for `structured_only` tenants is coarser, and their gate decisions are less explainable to a human reviewer. Two modes mean two test matrices and a per-tenant configuration surface.

**Neutral.** This ADR constrains the gate only. It is silent on whether lidless should ever *require* a third-party inference vendor — the design already assumes the gate is additive and fully removable, restoring today's behavior exactly.

## Alternatives considered

- **`structured_only` for everyone.** Rejected: it disables the most valuable judgment (fault class) for the tenant who needs remediation most, and it is stricter than lidless' own existing behavior, which already publishes redacted log text to third-party collaboration tools.
- **Send raw text and rely on the vendor.** Rejected outright — it contradicts the product's central promise.
- **Locally hosted model only.** Not viable today: no inference in the <100 MB budget, and the tripled node exists for observability, not for model serving. Retained as the revisit path if a fleet-hosted option appears.
