# ADR-0002 — Policy reconciliation by a model is advisory, never authoritative

**Status:** accepted (pending operator review) · **Date:** 2026-09-17 · **Deciders:** operator (operator), agent
**Supersedes:** — · **Related:** PLAN.md §2.3 Family C, §3 non-goal 2, L-014

## Context

`rules/**` changes currently pass through three checks: the backflow valve's HMAC signature (does this runtime mutation come from the genuine fast path?), JSON Schema validation (is the YAML well-formed?), and `scripts/sync_git_to_windmill.py` (push policy into Windmill).

None of them asks whether a change **makes sense**. A `pro`-tier tenant acquiring a `pagerduty_routing_key`, a silencing window with no expiry, or a remediation budget raised from 3 to 300 would all pass CI today, because they are syntactically valid and may carry a valid signature.

A System One model can answer that class of question well: it is a bounded semantic judgment over structured state, which is exactly what Jev is for. But admitting a model to a policy path invites a specific failure: the model becomes load-bearing for correctness of the *pipeline*, so a vendor outage blocks merges, a threshold becomes an authorization rule, and an external party ends up holding merge authority over the fleet's policy.

Standing doctrine already answers this: an external model adapter is an **asynchronous, untrusted advisory participant** — not an always-on worker, authorization plane, CI dependency, or merge authority.

## Decision

1. **The HMAC remains the authority on *who* wrote a change; the schema remains the authority on *form*; code and human review remain the authority on *permission*.** The model's opinion is never the deciding factor on any of the three.
2. **The review job always exits 0.** Every path — including a gate error, timeout, or missing credential — is a success for the build. Schema validation stays the only blocking check.
3. **It is not added to branch protection, required status checks, or any deploy gate**, and removing the job leaves the pipeline fully functional in both directions.
4. **Output is a labelled advisory comment** naming the model version, the question set, and the reasoning trail, addressed to a human who adjudicates.
5. **Diff-derived state only**, redacted to key names for tenants in `structured_only` mode (ADR-0001).
6. **Fast-path behavior is untouched:** signed runtime mutations still short-circuit the pipeline before any review step runs.

## Consequences

**Positive.** Policy review gains a second reader without granting it power. Vendor risk is contained: worst case is a missing comment. The same boundary generalizes to any future advisory participant.

**Negative.** An advisory can be ignored, and the first reviews will likely be noisy while the question set is tuned. Disagreements between the model and a human are not automatically captured anywhere unless L-014's comment links to the decision trail.

**Neutral.** This ADR does not prevent a *future* decision to make a specific check blocking — it requires that decision to be made explicitly, as its own ADR, with the model's role in it stated.

## Alternatives considered

- **Blocking check on high-confidence violations.** Rejected: it converts a probabilistic judgment into an authorization rule, and couples merge availability to a third-party early-access service.
- **Run it post-merge as an alert instead of a PR comment.** Viable, but the point is to inform the reviewer *before* the change lands. Revisit if comment noise proves unmanageable.
- **No model in the policy path at all.** Defensible. Rejected only because the review is genuinely useful and the advisory framing costs little; if the first calibration shows no signal, this ADR is the one to overturn.
