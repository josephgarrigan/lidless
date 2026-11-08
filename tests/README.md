# tests/ — L-001 fixture corpus and regex baseline

Offline. No network, no TypeSafe credentials, and nothing leaves the process: the baseline harness stubs the router's two dispatch functions before calling the real `tenant_router.main()`.

## Run it

```bash
python3 tests/fixtures/generate_corpus.py   # regenerate corpus (deterministic)
python3 tests/baseline_regex.py             # run the CURRENT engine + router, print report
python3 tests/test_policy_resolution.py     # policy index + issue #6 regression checks
python3 tests/test_derive_state.py          # derived-state contract (L-003)
python3 tests/test_questions_loader.py      # schema validation (L-004), no SDK needed

# These two call the live API, so they need the interpreter that has typesafe_sdk:
~/projects/typesafe/.venv/bin/python tests/test_questions_loader.py   # adds build + live call
~/projects/typesafe/.venv/bin/python tests/jev_accuracy.py            # L-002 measurement
~/projects/typesafe/.venv/bin/python tests/jev_accuracy.py --analyze  # re-report, no calls
```

Both live harnesses check for `TYPESAFE_API_KEY` (environment, else `~/.hermes/.env` on this workstation) and exit 2 with a clear message rather than a traceback when the interpreter or the key is missing.

`tests/out/baseline.json` is the recorded baseline. It is committed so a reviewer can diff behaviour before and after any change to `anecdote_engine.py` or `tenant_router.py`.

## What is in the corpus

58 fixtures, each a full Vector event envelope of the shape the README documents (`tenant_id`, `severity`, `is_error`, `tenant`, `event_meta`, `host_meta`).

- **`single_pattern` (40)** — 8 per `ERROR_PATTERNS` class, drawn from the phrasings each regex actually matches. Some carry fake credentials, internal IPs, and file paths on purpose, so L-003's sanitizer tests have material.
- **`multi_pattern` (5)** — messages matching two classes, where the *human* judgement picks a class that first-match-wins does not. These measure the ordering defect.
- **`novel` (3)** — real incidents no pattern matches (disk quota, certificate clock skew, a slow consumer). Expected class is `UNCLASSIFIED`.
- **`benign` (10)** — non-error events. `fault_class: NONE`; must never be routed and must never produce remediation.

Labels live in `labels.yaml`, human-assigned with a rationale each. `remediation_expected` is a judgement about whether a container restart is *appropriate* — not a restatement of what the rule does. That distinction is the point: it is the number L-008 calibrates thresholds against.

## Baseline as recorded (post issue #6 fix)

Classification across the 43 scored error fixtures:

- **8 of 8 for every known class.** The five patterns themselves are not where the engine fails.
- **Ordering test on the AMBIGUOUS set: 3 of 5 misresolved.** `multi_pattern_02` (intended auth) → `DB_CONNECTION_EXHAUSTION`, `_03` (upstream timeout) → `DB_CONNECTION_EXHAUSTION`, `_04` (panic) → `OOM_MEMORY_CEILING`. Cause: the loop `break`s on the first regex hit, and the list order is DB → OOM → AUTH → UPSTREAM → PANIC. These fixtures carry no agreed label (see the Issue #7 ruling below), so they are excluded from accuracy figures and used only for this test.
- **Novel faults swallowed: 3 of 3.** All three become `GENERIC_APPLICATION_FAULT`, narrated as a generic 500 at severity `HIGH` derived from `status_code`.
- **Engine severity has no effect on anything.** It diverges from the envelope severity on 38 of 43 scored error events, and the router reads `event_payload["severity"]` for both the `severity_overrides` lookup and the remediation condition. The engine's uppercase `severity` appears only inside narration strings.

### Remediation, now that policies actually resolve

8 of the 43 scored error fixtures trigger `restart_container` (all on `fintech`, the only tenant with remediation enabled). Agreement with human labels is 27 of 43, and the disagreements split cleanly:

- `fired_on_severity_string_alone = 4` — **live false positives on a destructive action.** Four `RUNTIME_PANIC` fixtures on `fintech` carry `severity: critical` in the envelope, so the rule restarts the container; the labels say a restart is not appropriate for a runtime panic. A restart is the *wrong* remedy for a code fault, and it is authorized by one string comparison.
- `severity_below_critical_floor = 4` — false negatives. Four `DB_CONNECTION_EXHAUSTION` fixtures on `fintech` declare `severity: error`, so no restart even though the labels say pool exhaustion is exactly what a restart clears.
- `policy_disabled_for_tenant = 8` — expected: `alpha-corp` has `auto_remediation.enabled: false`, and no `alpha-corp` fixture triggers anything.

Restricted to `fintech` (the one tenant with remediation enabled): 14 of 22 agree.

Routing, post-fix: both `matrix_anecdote_sent` and `slack_anecdote_sent` fire for `fintech` (Slack webhook + its own Matrix room) and `matrix_anecdote_sent` alone for `alpha-corp`.

**Correction (2026-09-20).** An earlier version of this file said "no benign event dispatches anything". That was wrong — it described an earlier version of the *harness*, which skipped the router for non-error events. The router has no `is_error` guard, so **all 10 benign fixtures are narrated and dispatched** (5 to Matrix, 5 to Slack), each as `GENERIC_APPLICATION_FAULT` at severity `MEDIUM`. The harness now calls the router for every event, matching production, and the baseline has been re-recorded. Filed as issue #8 with the policy decision it needs.

## L-002 — Jev over the corpus

`tests/jev_accuracy.py` sends the five-question gate schema to Jev for every fixture, twice, and compares against the labels and the L-001 baseline. Results in `tests/out/report_v1.md`; raw answers in `tests/out/jev_v1.json`. Re-run the analysis without spending calls using `--analyze`.

Headline (`jev-1.13.0`, 116/116 calls succeeded; figures over the 43 scored error fixtures):

- Classification: Jev 42/43 vs the regex baseline's 40/43. Two fixtures apart on 43, and both are the **novel** faults — Jev names them `unclassified` where the regex swallows them into a generic 500. Jev disagreed with a label on exactly one fixture (`novel_tls_clock_skew`, called `auth_security_rejection` at confidence 1.00), which is a label dispute rather than a model error: a certificate-validity failure *is* a security rejection.
- Confidence is informative: 41/42 agreement on fixtures answered at confidence ≥0.95, 2/6 below it. Jev was least confident on the fixtures whose labels are most arguable.
- Severity runs one band high on the middle band: 12 fixtures labelled `error` were scored `critical`.
- All 10 benign fixtures scored `not_an_incident`.
- Cost $0.00003777 per call (899 tokens), p50 217 ms, p95 295 ms.
- The `0.85 / 0.15` preview thresholds carried over from PLAN.md §2.3 are **void**: Jev's `restart_would_help` on the restart-labelled fixtures sits near 0.4 and `restart_discards_state` near 0.75. They must be derived from shadow data (L-008), never defaulted by intuition.

## L-007 — built

**The shadow gate.** `decision_gate.evaluate(..., shadow=True)` evaluates even when the tenant's gate is disabled, records the verdict on `would_be_action` / `would_be_code`, and returns `action="none"` **unconditionally** — structural, not conventional: whatever the judgment was, the caller cannot receive an action. `shadow` is an independent policy switch from `enabled` (the authority switch), so shadow can run for the whole shadow period while the gate stays off in git, as the plan requires.

**The ledger** (`windmill/flows/shadow_ledger.py`) writes one JSON line per event to `<archive>/<bucket>/<prefix>/shadow-<date>.jsonl`, resolving the archive from the tenant's own `s3_archive_prefix` and falling back to the platform tenant's per the OD-4 ruling. It is an allow-list, not a filter: fields not named in `LEDGER_FIELDS` are never written, which makes "no log content" a property of the format rather than a promise. `reason_detail_full` is the single exception and only for the tenant's own archive. Every failure path returns a status dict; the module does not raise into a dispatch path.

**Router wiring** appends `shadow_decision` to the response and wraps the entire shadow block in `try/except`, so a gate failure is logged and contained.

`tests/test_shadow_wiring.py` — 37 checks. It tests the claim twice: against recorded history (`dispatched_to` still matches the L-001 baseline for all 58 fixtures) and against itself (every response key except `shadow_decision` is identical between a shadow-off pass and a shadow-on pass). Plus: one ledger line per event, no message text anywhere in any line in either state mode, archive resolution including the fallback and the unassigned case, a broken ledger root that leaves dispatch intact, a silenced tenant that produces no call and no line, and a misconfigured gate (`jev-latest`) that is contained and surfaced.

**Measured on the corpus**, with the answers L-002 actually observed:

- 11 fixtures trigger the current rule's restart (all `fintech`): 4 `RUNTIME_PANIC`, 4 `OOM_MEMORY_CEILING`, 3 ambiguous multi-pattern.
- With permissive answers, the gate agrees wherever the rule acts, and records 19 `gate_would_act` disagreements where the rule stayed idle.
- With the measured answers (`help 0.40`, `discards 0.74`), the gate withholds **all 11** of those restarts — every one carrying its failing checks and its answer set, which is the evidence L-008 calibrates from.

**A design gap the tests exposed and I fixed:** the gate used to decline *before* calling the model whenever `auto_remediation.enabled` was false. Since `alpha-corp` and `platform` never auto-remediate, a shadow period would have gathered **zero** evidence for exactly the tenants whose thresholds most need justifying. In shadow, a configuration block is now no longer a reason to skip the judgment: the answers are recorded, and the action is still nothing.

**And the harness was hiding a defect** — see issue #8 above: the router has no `is_error` guard, so all 10 benign fixtures are dispatched as incidents.

## L-006 — built

**Schema.** `rules/schema/tenant-policy.schema.json` gains an optional `decision_gate` block: `enabled` (required, default false), `mode` (`shadow` | `enforce`), `model` (pinned — a pattern plus a `not: -latest$` guard), `state_mode` (ADR-0001's two egress modes), `questions_version`, `allowed_severities`, `on_low_confidence` (`escalate` | `route_only`), and `thresholds` keyed by **action** (`propertyNames` restricted to the real action names) with each threshold bounded to 0–1 and at least one required per action. `additionalProperties: false` on the block and on each threshold entry, so a typo is an error rather than a silently ignored field.

**Tenants.** All three carry a block, all three are **off**:

- `fintech` — `sanitized_content`, concrete thresholds for `restart_container`, `enabled: false`. The thresholds are annotated in the file as placeholders that L-002 voided, to be derived in L-008; until then the gate can only withhold.
- `alpha-corp` — `structured_only`. It is an external enterprise tenant whose contract we do not control, so redacted log text does not leave the fleet for a US inference vendor. The cost is fault-class granularity only.
- `platform` — `sanitized_content`, and the tenant L-009's rehearsal will run against, so it carries a full block rather than a stub.

That also means both egress modes are exercised by the fleet, so a mode-specific bug has somewhere to surface before a tenant depends on it.

**CI already covers it.** `reconcile-rules.yml` validates every `rules/tenants/*.yaml` against the schema on the slow path, and the loop is gate-agnostic — so the new block is validated on every PR without touching the workflow. Verified by running that exact loop locally: all three tenants pass.

`tests/test_policy_schema.py` — 78 checks. Every shipped tenant validates; stripping `decision_gate` still validates, proving the block is optional and the existing keys are untouched; the gate is off everywhere with a pinned model and a declared mode; fourteen invalid policies are rejected (alias model, unknown mode and state_mode, unversioned `questions_version`, missing `enabled`, an invented property, empty or invented `allowed_severities`, a threshold keyed to a non-action, an unknown threshold field, thresholds outside 0–1, an empty threshold block). `mode: enforce` with `enabled: false` validates and is demonstrated inert.

**The check that matters:** for each tenant, the test enables the gate and re-runs the real `decision_gate.evaluate()`. For `alpha-corp` and `platform` — whose `auto_remediation.enabled` is false — the reason is `remediation_not_enabled_by_policy`, so opting into the gate cannot authorize a remediation the tenant policy has not already permitted. For `fintech`, where remediation *is* enabled, the gate may decide.

**A pre-existing gap this found:** `max_restarts_per_hour` accepted a negative integer, so a policy could declare a nonsense budget and validate. Fixed with `minimum: 0`, caught by the new negative-case checks rather than by review.

## L-005 — built

**`windmill/flows/decision_gate.py`.** `evaluate(event_payload, tenant_policy) -> Decision`: it asks the versioned questions about a derived state, applies the tenant's per-action thresholds, and returns a proposal. It dispatches nothing, restarts nothing, writes nothing — the caller decides what to do with the answer, and the hourly remediation budget stays with the caller (L-009) so this remains a pure judgment function.

Two invariants hold by construction and are asserted:

- **Fail closed.** Every error class returns `action="none"` with `decision_unavailable=True`. There is no path from an exception to an action.
- **Policy wins.** The action only ever comes from tenant policy (`auto_remediation.target_action`); the model can *withhold* it and never grant it. A severity outside `allowed_severities` withholds regardless of confidence.

Answers are read by duck-typing (`choice` / `score` / `noul` attributes) rather than by importing the SDK's answer classes, so the whole gate is testable with a stub and under plain `python3` — and a Noul answer is never given a confidence field it does not have.

`tests/test_decision_gate.py` — 67 checks passing offline, covering the happy path, each threshold withholding individually, `route_only` versus `escalate`, every configuration state (gate disabled, no thresholds for the action, `state_mode` undeclared, remediation off, `target_action: none`), all five SDK error classes, a missing answer, decision-id stability across identical state and sensitivity to window and fault class, and a source scan proving the module contains no dispatch, subprocess, or HTTP path of its own.

**Live run (`--live`, real SDK, real schema, real API).** On `oom_memory_ceiling_01` — the exact event where `tenant_router` restarts the container today:

```
action='none'
reason=low_confidence_or_policy: restart_would_help 0.48 < 0.85; restart_discards_state 0.74 > 0.15
fault_class='oom_memory_ceiling' severity='critical' sev_conf=0.99
model=jev-1.13.0 tokens={'input_tokens': 985, 'output_tokens': 184} latency=1493ms
```

The gate withholds precisely where the current rule acts, and says why. The payload sent contains the sanitized preview and no `raw_message` field — the boundary from ADR-0001, verified on the wire rather than in a stub.

**The explanation is structured now** (2026-09-20), so the shadow ledger and the DLQ have fields to count rather than prose to parse: `reason_code` from a fixed `REASON_CODES` vocabulary, `failed_checks` as `[{check, actual, threshold, comparison}]`, and a `reason_summary` bounded to `MAX_REASON_SUMMARY` (200 chars). When the summary has to be cut, the full text is kept on `reason_detail_full` for the ledger and the event carries `reason_detail_ref` — the content address `artifact://sha256/…` — instead. <!-- id-contract: mention --> `Decision.envelope()` is what goes on the wire (no unbounded prose); `Decision.to_dict()` is the complete ledger record. In practice a one-check explanation fits and a four-check one does not, which is the bound doing its job: the structured checks already carry the detail, so nothing is lost.

`tests/test_decision_gate.py` — 96 checks, and the restructure caught two real bugs in my own first pass: the structured checks were being discarded on every decline path, and the detail pointer was being set unconditionally rather than only when the prose did not fit.

## Latency benchmark

`tests/bench_latency.py` — 37 real calls against the shipped schema, `tests/out/bench_latency.json`:

```
full schema (5 questions)    n=24  p50=212  p90=279  p95=303  p99=307  max=307  sd=39   tokens/call=985
control (1 question)         n=12  p50=195  p90=234  p95=234  p99=275  max=275  sd=33
median delta from adding 4 questions: 17 ms (1.09x)     inside the advertised 70-500 ms band: 24/24
cold start (first call): 1521 ms
```

**Three findings:**

1. **Steady state is 212 ms at p50, 303 ms at p95 — inside the band, 24 of 24 calls.** The 1493 ms I reported in L-005 was a *cold start*, not the schema's cost. That was my misreading, and a single sample could never have settled it; the first call in a process costs ~1.5 s, every call after it costs ~200 ms. Practical consequence for L-007: the worker should warm the connection at startup rather than discovering this on the first real incident.
2. **The vendor's parallel-question claim holds.** Five questions cost 9% more median latency than one (212 ms vs 195 ms) — 17 ms for four extra judgments. The article's "adding questions barely changes response time" is corroborated here rather than taken on faith.
3. **Cost is $0.00004137 per call** at 985 tokens, slightly above the $0.0000378 measured for L-002's leaner inline schema — the difference is the fuller criteria text in the shipped YAML, which is a deliberate trade.

The gate's own overhead is negligible beside the API call: derivation and thresholding are pure in-process work.

## L-003 / L-004 — built

**L-003 (`windmill/flows/decision_state.py`).** `sanitize_message()` moved out of `anecdote_engine` — where it ran *after* pattern matching, so ordering was an accident of function layout — into one implementation that runs before any egress. `anecdote_engine` re-exports it, so existing callers are unchanged. `derive_state(event_payload, mode)` returns only fields that already exist in the envelope and never copies `raw_message` out of `event_meta`. `mode` has no default: omitting it is a `TypeError`, and an unknown value raises `StateModeError`. `state_fingerprint()` gives L-011's decision cache a stable key.

`tests/test_derive_state.py` — 27 checks, all passing: five redaction cases (IPv4, bearer token, `token:`, path prefix, `key=`), the re-export identity, five bad modes plus the omitted argument, the no-raw-token-escapes invariant for `structured_only` (scanning the serialised state for every message token the envelope does *not* carry as a structured field), preview equality and capping, and non-mutation of the input.

**One finding worth carrying into L-011.** In `structured_only` the message is deliberately absent from the state, so **two different faults in the same container at the same status code produce an identical fingerprint**. That is correct behaviour and it is exactly the hazard L-011's guardrail names: the decision cache must not collapse two distinct faults into one decision. The test asserts the property explicitly so the cache cannot quietly rely on the fingerprint to separate them.

**L-004 (`windmill/flows/jev/questions.v1.yaml` + `loader.py`).** Questions are data, not code: changing wording is a YAML edit, and `questions_version` rides into every audit envelope. `validate_schema()` runs without the SDK (so CI and the plain-python tests do not need it), while `build_questions()` imports `typesafe_sdk` lazily. Validation refuses: a missing version, a `-latest` alias model, unknown question types, empty `instructions`, a Choice with no criteria, a Choice with **no `no_match_option`**, a no-match option that is not one of the criteria, more than 255 options, a Noul whose criteria lacks a side, and a question that mentions **its own id** in its text — that last one because ids are never sent to the model, so such text is meaningless to it.

`tests/test_questions_loader.py` — 18 checks. The live section runs the shipped schema end to end against the API through `derive_state(..., "sanitized_content")`: all five answers returned, model pinned, and the OOM fixture resolved as `fault_class=oom_memory_ceiling`, `severity=critical`, `help=0.49`, `discards=0.74` at 985 input tokens.

Worth noting for cost: the YAML criteria are fuller than the inline schema L-002 measured, and cost **985 tokens against 899** — 86 tokens per call for better-described options, or about $0.0000036. Cheap, but it scales with schema prose, not with the log line.

## Issue #7 — adjudicated 2026-09-20

Three rulings from the operator, all applied:

1. **Ambiguous dispositions go to a dead-letter queue**, not to a forced yes/no. The queue is staged: a folder of files now, tags over the queue later, a Matrix room eventually. A queue of individual failures is a to-do list; a *tagged* queue is a procedure-discovery mechanism — where the remediation procedure for a recurring condition gets written once, by whoever watched it happen. Recorded in `PLAN.md` §2.7.1; L-010 builds it.
2. **The five multi-pattern fixtures are marked `AMBIGUOUS`**, carrying an `intended_class` for the ordering test and excluded from every accuracy figure. See the baseline section above.
3. **Thresholds come from L-008**, confirmed. The `0.85 / 0.15` values in `PLAN.md` §2.3 are annotated as void.

## Issue #6 — fixed

`load_tenant_policy` built its path as `rules/tenants/<tenant_id>.yaml`, but the shipped files were named `fintech-team.yaml` (declaring `tenant_id: "fintech"`) and `enterprise-alpha.yaml` (declaring `tenant_id: "alpha-corp"`). Neither lookup could succeed, so **every tenant silently fell back to the platform policy** — all 48 error fixtures dispatched to the platform room, tenant sinks and `severity_overrides` were dead, and auto-remediation was off by accident rather than design.

Fixed by making the **declared `tenant_id` authoritative**:

- `build_policy_index()` indexes `rules/tenants/*.yaml` by declared id, cached, with `force=True` to rebuild. A file declaring no `tenant_id`, or two files declaring the same one, raises `TenantPolicyError` — an ambiguous index is a hard error, because silently picking one of two policies for a tenant is how a routing change becomes invisible.
- `resolve_policy_source(tenant_id)` returns `(path, resolved)`; `resolved=False` is the fallback, and it is logged at WARNING. The decision-gate path will use `strict=True` so a decision is never taken under another tenant's policy.
- The three shipped files were renamed to match their declared ids (`fintech.yaml`, `alpha-corp.yaml`, `platform.yaml`), because `git_backflow_valve.py` already writes runtime mutations to `rules/tenants/<tenant_id>.yaml` — without the rename, the first runtime policy mutation would have created a second file declaring an existing tenant.
- `tests/test_policy_resolution.py` covers resolution, filename agreement, the loud fallback, `strict=True`, and duplicate detection.

Any behaviour figure recorded before this fix is void: the earlier `remediation agreement: 27/48` was entirely the fallback, not the severity floor.
