# ==============================================================================
# Windmill Flow: tenant_remediate (Dynamic Multi-Tenant Router + Anecdote Post-Processor)
# ==============================================================================
# Triggered by Vector HTTP webhook in real-time.
# 1. Matches event against tenant policy cache (rules/tenants/<tenant_id>.yaml).
# 2. Synthesizes a human-readable runtime anecdote using anecdote_engine.
# 3. Dispatches customized alert payload to unique org/team sinks (Matrix/Slack/PD).
# 4. Triggers isolated auto-remediation if authorized.

import os
import json
import time
import logging
import yaml
import glob
import requests
from pathlib import Path
from windmill.flows.anecdote_engine import synthesize_anecdote
from windmill.flows.decision_gate import (
    GateError, evaluate as evaluate_gate, authorize_remediation,
)
from windmill.flows.shadow_ledger import record as record_shadow
from windmill.flows.shadow_ledger import rule_outcome_from

log = logging.getLogger("lidless.tenant_router")

POLICY_DIR = os.environ.get("TENANT_POLICY_DIR", "/opt/observability/rules/tenants")
FALLBACK_TENANT_ID = "platform"

# One retry, then stop. Injectable so a test does not sleep.
DEFAULT_RATE_LIMIT_BACKOFF_S = 1.0  # fallback if not in decision_gate policy

# The declared `tenant_id` is authoritative, not the filename. The backflow valve
# writes runtime mutations to rules/tenants/<tenant_id>.yaml, so a runtime change
# and a hand-authored file could otherwise declare the same tenant twice.
_policy_index = None


class TenantPolicyError(RuntimeError):
    """Raised when the policy index is ambiguous or a required policy is missing."""


def build_policy_index(force: bool = False) -> dict:
    """Index every policy in POLICY_DIR by its declared tenant_id.

    Raises TenantPolicyError if two files declare the same tenant_id, or if a
    file declares none. An ambiguous index is a hard error: silently choosing
    one of two policies for a tenant is how a routing change becomes invisible.
    """
    global _policy_index
    if _policy_index is not None and not force:
        return _policy_index

    index = {}
    for path in sorted(glob.glob(os.path.join(POLICY_DIR, "*.yaml"))):
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        tenant_id = data.get("tenant_id")
        if not tenant_id:
            raise TenantPolicyError(f"{path} declares no tenant_id")
        if tenant_id in index:
            raise TenantPolicyError(
                f"duplicate tenant_id {tenant_id!r} declared by both "
                f"{index[tenant_id]} and {path}"
            )
        if os.path.basename(path) != f"{tenant_id}.yaml":
            log.warning(
                "policy %s declares tenant_id %r; filename does not match. "
                "The declared id wins, but runtime backflow will write %s.yaml",
                path, tenant_id, tenant_id,
            )
        index[tenant_id] = path

    _policy_index = index
    return index


def resolve_policy_source(tenant_id: str) -> tuple:
    """Return (path, resolved) for a tenant. `resolved` is False on fallback.

    Recorded in the decision envelope so a reviewer can tell which policy was
    actually in force - the defect fixed in issue #6 was invisible precisely
    because nothing reported this.
    """
    index = build_policy_index()
    if tenant_id in index:
        return index[tenant_id], True
    fallback = index.get(FALLBACK_TENANT_ID)
    if fallback is None:
        raise TenantPolicyError(
            f"no policy declares tenant_id {tenant_id!r}, and no fallback "
            f"tenant {FALLBACK_TENANT_ID!r} exists in {POLICY_DIR}"
        )
    return fallback, False


def load_tenant_policy(tenant_id: str, strict: bool = False) -> dict:
    """Load a tenant's policy, or the fallback tenant's policy, loudly.

    strict=True raises instead of falling back - use it on paths where running
    under another tenant's policy would be a cross-tenant incident (issue #6).
    """
    path, resolved = resolve_policy_source(tenant_id)
    if not resolved:
        if strict:
            raise TenantPolicyError(
                f"no policy declares tenant_id {tenant_id!r}; refusing to fall "
                f"back to {FALLBACK_TENANT_ID!r} in strict mode"
            )
        log.warning(
            "no policy declares tenant_id %r; falling back to %s",
            tenant_id, os.path.basename(path),
        )

    with open(path, "r") as f:
        policy = yaml.safe_load(f) or {}

    if not policy:
        log.warning("policy %s is empty; routing will fall back to stdout", path)
        return {"tenant_id": tenant_id, "silenced": False, "routing": {"default_sink": "stdout"}}
    return policy

def dispatch_matrix_anecdote(room_id: str, anecdote: dict, tenant_policy: dict):
    synapse_url = os.environ.get("SYNAPSE_HOMESERVER_URL", "https://matrix.domain.tld")
    token = os.environ.get("GATUS_MATRIX_TOKEN", "")
    if not token or not room_id:
        return

    bullet_text = "\n".join([f"• {b}" for b in anecdote["bulletins"]])
    body = (
        f"📢 **{anecdote['headline']}**\n\n"
        f"📖 **Runtime Narrative:**\n{anecdote['narrative']}\n\n"
        f"📝 **Log Extract (Sanitized):**\n`{anecdote['sanitized_preview']}`\n\n"
        f"📌 **Key Insights:**\n{bullet_text}\n\n"
        f"🔍 **Inspect Live Container Logs:** {anecdote['dozzle_drilldown_url']}"
    )

    url = f"{synapse_url}/_matrix/client/r0/rooms/{room_id}/send/m.room.message?access_token={token}"
    requests.post(url, json={"msgtype": "m.text", "body": body}, timeout=5)

def dispatch_slack_anecdote(webhook_url: str, anecdote: dict, tenant_policy: dict):
    if not webhook_url:
        return

    bullet_text = "\n".join([f"• {b}" for b in anecdote["bulletins"]])
    payload = {
        "text": f"*{anecdote['headline']}*",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": anecdote["headline"][:150]
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Story:* {anecdote['narrative']}\n\n*Log Snippet:* `{anecdote['sanitized_preview']}`"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Insights:*\n{bullet_text}"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Inspect in Dozzle"},
                        "url": anecdote["dozzle_drilldown_url"],
                        "style": "primary"
                    }
                ]
            }
        ]
    }
    requests.post(webhook_url, json=payload, timeout=5)


def perform_restart(container, action):
    """The restart trigger. The marker string is the contract the router has
    always emitted. Injected so a test can count calls. This does not touch a
    container — a live restart is the rehearsal, not this function.
    """
    return "triggered_remediation_" + str(action) + "_on_" + str(container)


def enqueue_dlq(record, *, directory=None):
    """Stage-1 DLQ: one file per item, nothing parsed, no log content.

    L-010 owns tags, the Matrix notice, and requeue. This is the folder the
    failure table already requires. GATE_DLQ_DIR names it; without that, the
    record is logged and returned with queued=false rather than invented.
    A mkdir or write failure is queued=false and a log line, not an exception:
    dispatch has already happened.
    """
    out = dict(record or {})
    out.pop("raw_message", None)
    log.warning(
        "gate dlq class=%s decision_id=%s attempts=%s",
        out.get("error_class"), out.get("decision_id"), out.get("attempt_count"),
    )
    directory = directory or os.environ.get("GATE_DLQ_DIR")
    if not directory:
        out["queued"] = False
        return out
    folder = Path(directory)
    # Stem and attempt are alphanumerics only, so neither can escape GATE_DLQ_DIR.
    stem = "".join(c for c in str(out.get("decision_id") or "undecided") if c.isalnum()) or "undecided"
    attempt = "".join(c for c in str(out.get("attempt_count") or 0) if c.isalnum()) or "0"
    target = folder / (stem + "-" + attempt + ".json")
    blob = json.dumps({k: v for k, v in out.items() if k != "queued"}, sort_keys=True)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target.write_text(blob + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("gate dlq write failed: %s", exc)
        out["queued"] = False
        return out
    out["queued"] = True
    out["path"] = str(target)
    return out


def alert_platform(record, room_id=None):
    """Sanitized notice for a bad request. The request id, never the event body.

    L-010: the log line is not the notice. The platform room gets the
    allow-listed line. No room means the log stands and nothing is invented.
    The room is the platform tenant's, looked up when the caller does not
    pass one, so an injected one-argument alert still matches today's tests.
    """
    log.warning(
        "gate platform alert request_id=%s code=%s",
        record.get("request_id"), record.get("reason_code"),
    )
    if room_id is None:
        try:
            policy = load_tenant_policy("platform")
        except Exception:  # noqa: BLE001 - a bad policy file must not abort dispatch
            policy = None
        room_id = ((policy or {}).get("routing") or {}).get("matrix_room_id")
    try:
        from windmill.flows import gate_dlq
        gate_dlq.notify_platform(record, room_id)
    except Exception as exc:  # noqa: BLE001 - the notice is not the response
        log.warning("gate platform notice failed: %s", exc)
    return record


def _dlq_record(decision, attempts):
    """Built from the decision, not the event, so a raw line cannot leak in."""
    return {
        "error_class": getattr(decision, "reason_code", None),
        "decision_id": getattr(decision, "decision_id", None),
        "attempt_count": attempts,
        "request_id": getattr(decision, "request_id", None),
        "tenant_id": getattr(decision, "policy_source", None),
    }


def _alert_record(decision, tenant_id):
    return {
        "request_id": getattr(decision, "request_id", None),
        "reason_code": getattr(decision, "reason_code", None),
        "tenant_id": tenant_id,
        "decision_id": getattr(decision, "decision_id", None),
    }


def _budget_directory(directory):
    """The argument, else GATE_BUDGET_DIR. Empty is missing."""
    if directory is None:
        directory = os.environ.get("GATE_BUDGET_DIR")
    if not directory:
        return None
    return directory


def _budget_path(tenant_id, directory):
    hour = int(time.time() // 3600)
    stem = "".join(c for c in str(tenant_id) if c.isalnum()) or "tenant"
    return Path(directory) / (stem + "-" + str(hour))


def load_restarts_this_hour(tenant_id, *, directory=None):
    """Measured restarts for this tenant and hour, or None if unmeasured.

    A readable directory with no file for this hour is 0. A missing directory,
    or OSError while reading, is None. Unknown is not zero.
    """
    directory = _budget_directory(directory)
    if directory is None:
        return None
    folder = Path(directory)
    path = _budget_path(tenant_id, directory)
    try:
        if not folder.is_dir():
            return None
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Looked. No file for this hour. Measured 0, not an assumption.
        return 0
    except OSError as exc:
        log.warning("hourly budget unreadable for tenant %s: %s", tenant_id, exc)
        return None
    return len(text.splitlines())


def append_budget_line(tenant_id, *, directory=None):
    """Append one budget line. False if it does not land. Does not raise."""
    directory = _budget_directory(directory)
    if directory is None:
        return False
    path = _budget_path(tenant_id, directory)
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("restart " + str(int(time.time() // 3600)) + "\n")
    except OSError as exc:
        log.warning("hourly budget append failed for tenant %s: %s", tenant_id, exc)
        return False
    return True


def main(event_payload: dict, *, gate_client=None, gate_schema=None, ledger_root=None,
         shadow_notify: bool = False, restarts_this_hour: int | None = None, sleep=None):
    """Route one event.

    Two severities are in play and they are not interchangeable (OD-3, lidless#3): the *reported*
    one, from whichever detector matched first, and the *gate's*, which is a judgement about
    whether this is worth waking someone for. The reported severity is always recorded as
    `reported_severity`; whether the gate's severity drives routing depends on the tenant granting
    the gate authority - `enabled`, enforce mode, and no `shadow` flag. During a shadow period the
    gate can neither change the dispatch set nor fail the call (L-007), and the envelope records
    what it would have said instead.

    The gate never *grants* an action. Without authority, remediation still keys on the
    reported severity — that is today's path, and enabled:false returns to it. With
    authority, a restart also needs the gate's authorization and a remaining hourly
    budget, checked here as well as in authorize_remediation. A count of None loads
    the file counter (GATE_BUDGET_DIR, tenant id plus hour). An explicit int is
    used as given. Unknown withholds; it is not treated as zero.

    The gate is injected here so tests can run it against a stub.
    """
    tenant_id = event_payload.get("tenant_id", "global")
    reported_severity = event_payload.get("severity", "error")
    event_meta = event_payload.get("event_meta", {}) or {}

    # 1. Load tenant policy
    policy = load_tenant_policy(tenant_id)

    # 2. Check tenant silence window
    if policy.get("silenced", False):
        return {"status": "silenced", "tenant_id": tenant_id}

    gate_policy = policy.get("decision_gate", {}) or {}

    # 3. is_error stopgap (lidless#8, option A). The router cannot infer that a log line is not
    #    an error, and until the gate is answering that question a benign line arrives here
    #    looking exactly like an incident. Opt-in per tenant rather than a default in code:
    #    what is worth telling a tenant about is policy, and policy belongs where changing it is
    #    reviewable. Suppression is reported in the response rather than only logged, because a
    #    silent filter is the mirror of the noise it removes.
    if gate_policy.get("is_error_stopgap", False) and event_meta.get("is_error") is not True:
        return {"status": "not_an_incident", "tenant_id": tenant_id,
                "reported_severity": reported_severity,
                "suppressed_by": "is_error_stopgap",
                # Every response carries dispatched_to, so a caller reads one field for "did
                # anything go out" no matter which path ended the call.
                "dispatched_to": []}

    # 4. Post-Process Runtime Telemetry into Human Anecdote
    verbosity = policy.get("anecdote_verbosity", "user_friendly")
    anecdote = synthesize_anecdote(event_payload, verbosity=verbosity)

    # 5. Decision gate, evaluated BEFORE dispatch because in enforce mode its severity is the
    #    routing predicate (OD-3). Triggered by `shadow` (observe while the gate is off) or by
    #    `enabled` (the authority switch); both are independent keys by design. Non-fatal: a
    #    gate failure routes on the reported severity and says so, rather than dropping the event.
    mode = gate_policy.get("mode", "shadow")
    gate_shadow = bool(gate_policy.get("shadow", False))
    gate_enabled = bool(gate_policy.get("enabled", False))
    # Authority to change routing needs `enabled` AND enforce mode AND no shadow flag. A shadow
    # period that altered the dispatch set would not be a shadow period (L-007).
    gate_has_authority = gate_enabled and not gate_shadow and mode == "enforce"

    platform_policy = None
    decision = None
    gate_error = None
    enforce_side_effect = None
    rate_limit_retried = False
    auth = None
    fired = False
    if gate_shadow or gate_enabled:
        try:
            platform_policy = load_tenant_policy("platform")
        except TenantPolicyError:
            platform_policy = None
        try:
            decision = evaluate_gate(event_payload, policy, client=gate_client,
                                     schema=gate_schema, shadow=not gate_has_authority)
            decision.reported_severity = reported_severity
        except Exception as exc:  # noqa: BLE001 - the gate must never break dispatch
            log.warning("decision gate failed for tenant %s: %s", tenant_id, exc)
            decision = None
            gate_error = f"{type(exc).__name__}: {exc}"

    # 429: one retry with backoff, then the decision we have is the one we keep.
    # A bad request is never retried. The judgment function itself stays single-shot.
    # Backoff comes from decision_gate.rate_limit_backoff_s (L-010), defaulted here.
    rate_limit_retried = False
    if decision is not None and decision.reason_code == "gate_rate_limited":
        backoff = gate_policy.get("rate_limit_backoff_s", DEFAULT_RATE_LIMIT_BACKOFF_S)
        (sleep or time.sleep)(backoff)
        try:
            retried = evaluate_gate(event_payload, policy, client=gate_client,
                                    schema=gate_schema, shadow=not gate_has_authority)
            retried.reported_severity = reported_severity
            decision = retried
            rate_limit_retried = True
        except Exception as exc:  # noqa: BLE001 - keep the first failure, do not invent a second
            log.warning("decision gate retry failed for tenant %s: %s", tenant_id, exc)
            rate_limit_retried = True
            gate_error = type(exc).__name__ + ": " + str(exc)

    gate_severity = decision.severity if decision is not None else None
    if gate_has_authority and gate_severity == "not_an_incident":
        return {"status": "not_an_incident", "tenant_id": tenant_id,
                "reported_severity": reported_severity, "gate_severity": gate_severity,
                "suppressed_by": "decision_gate", "dispatched_to": []}
    routing_severity = (gate_severity or reported_severity) if gate_has_authority else reported_severity

    # 5b. Enforce-mode flags, applied to the anecdote before it is routed.
    # Low confidence escalates. An unavailable judgment is flagged. A bad
    # request and a spent rate-limit retry name their own side effect; the
    # sinks run after dispatch, once we know a restart did not fire.
    enforce_side_effect = None
    if gate_has_authority and decision is not None:
        anecdote = synthesize_anecdote(
            event_payload, verbosity=verbosity, fault_class=decision.fault_class, decision=decision
        )
        if decision.reason_code == "gate_rate_limited":
            enforce_side_effect = "dlq"
        elif decision.reason_code == "gate_bad_request":
            enforce_side_effect = "alert_platform"
        elif decision.decision_unavailable:
            enforce_side_effect = "flag"
        elif decision.escalated and decision.reason_code in (
                "threshold_failed", "threshold_failed_and_severity_not_permitted"):
            enforce_side_effect = "escalate"
            headline = anecdote.get("headline") or ""
            if not str(headline).startswith("REVIEW REQUIRED"):
                anecdote["headline"] = "REVIEW REQUIRED: " + str(headline)
            anecdote["review_required"] = True
        if decision.decision_unavailable:
            anecdote["decision_unavailable"] = True
            bulletins = list(anecdote.get("bulletins") or [])
            bulletins.append("decision_unavailable: true")
            anecdote["bulletins"] = bulletins

    # 6. Multi-Tenant Fan-Out Dispatching
    routing = policy.get("routing", {})
    overrides = policy.get("severity_overrides", {})
    target_sinks = overrides.get(routing_severity, [routing.get("default_sink", "matrix")])

    results = []
    if "matrix" in target_sinks and routing.get("matrix_room_id"):
        dispatch_matrix_anecdote(routing["matrix_room_id"], anecdote, policy)
        results.append("matrix_anecdote_sent")

    if "slack" in target_sinks and routing.get("slack_webhook_url"):
        dispatch_slack_anecdote(routing["slack_webhook_url"], anecdote, policy)
        results.append("slack_anecdote_sent")

    # 7. Remediation. Without authority this is today's path: reported critical, and a
    #    policy that already permits the action. With authority the gate must also have
    #    authorized, and the hourly budget is checked again here — the router does not
    #    trust authorize_remediation's word alone. Reported severity still gates the
    #    grant: a gate that says critical does not restart an event the detector did not.
    #    None loads the hourly file counter. enabled:false / shadow does not.
    remediation = policy.get("auto_remediation", {}) or {}
    container = (event_payload.get("host_meta") or {}).get("container")
    action = remediation.get("target_action")
    auth = None
    fired = False
    # None means load the counter. An explicit int is the test injection and is
    # not replaced. The file counter is enforce-only.
    caller_supplied_count = restarts_this_hour is not None
    if gate_has_authority and not caller_supplied_count:
        restarts_this_hour = load_restarts_this_hour(tenant_id)
    if gate_has_authority:
        auth = authorize_remediation(
            decision, policy,
            reported_severity=reported_severity,
            restarts_this_hour=restarts_this_hour,
        )
        cap = remediation.get("max_restarts_per_hour")
        policy_still = (
            bool(remediation.get("enabled", False)) and action not in (None, "none", "")
        )
        budget_still = (
            isinstance(cap, int) and not isinstance(cap, bool) and cap >= 0
            and isinstance(restarts_this_hour, int) and not isinstance(restarts_this_hour, bool)
            and restarts_this_hour >= 0 and restarts_this_hour < cap
        )
        if auth.get("fire") and policy_still and budget_still:
            commit = caller_supplied_count
            if not caller_supplied_count:
                # Re-read before the restart marker. Unknown, a count that has
                # reached the cap, or a failed append does not fire.
                again = load_restarts_this_hour(tenant_id)
                commit = (
                    isinstance(again, int) and not isinstance(again, bool)
                    and again >= 0 and again < cap
                    and append_budget_line(tenant_id)
                )
            if commit:
                results.append(perform_restart(container, action))
                fired = True
                enforce_side_effect = "restart"
            elif enforce_side_effect is None:
                enforce_side_effect = "budget_exhausted"
        elif enforce_side_effect == "dlq" and decision is not None:
            dlq_rec = _dlq_record(decision, 2 if rate_limit_retried else 1)
            try:
                enqueue_dlq(dlq_rec)
            except Exception as exc:  # noqa: BLE001 - a surprise must not abort the response
                log.warning("gate dlq enqueue failed for tenant %s: %s", tenant_id, exc)
            # L-010: emit sanitized platform notice per DLQ record
            try:
                from windmill.flows import gate_dlq
                room_id = None
                try:
                    policy = load_tenant_policy("platform")
                except Exception:  # noqa: BLE001
                    policy = None
                room_id = ((policy or {}).get("routing") or {}).get("matrix_room_id")
                gate_dlq.notify_platform(dlq_rec, room_id)
            except Exception as exc:  # noqa: BLE001 - notice must not abort dispatch
                log.warning("gate dlq platform notice failed: %s", exc)
        elif enforce_side_effect == "alert_platform" and decision is not None:
            try:
                alert_platform(_alert_record(decision, tenant_id))
            except Exception as exc:  # noqa: BLE001 - a surprise must not abort the response
                log.warning("gate platform alert failed for tenant %s: %s", tenant_id, exc)
        elif decision is None and enforce_side_effect is None:
            enforce_side_effect = "flag"
        elif enforce_side_effect is None and auth.get("reason") == "policy_withheld":
            enforce_side_effect = "policy_withheld"
        elif enforce_side_effect is None and auth.get("reason") == "budget_exhausted":
            enforce_side_effect = "budget_exhausted"
    elif remediation.get("enabled", False) and reported_severity == "critical":
        results.append(perform_restart(container, action))

    # 8. Shadow record (L-007). Evaluated above, written here because the outcome is only known
    #    once dispatch has happened. Every failure is caught: shadow observes the pipeline, it
    #    does not participate in it.
    shadow = None
    if decision is not None:
        try:
            outcome = rule_outcome_from(results)
            ledger = record_shadow(decision, policy, outcome, platform_policy=platform_policy,
                                   event_payload=event_payload, root=ledger_root,
                                   room_id=(platform_policy or {}).get("routing", {}).get("matrix_room_id"),
                                   notify=shadow_notify)
            shadow = {
                "decision_id": decision.decision_id,
                "would_be_action": decision.would_be_action,
                "would_be_code": decision.would_be_code,
                "reason_code": decision.reason_code,
                "reason_summary": decision.reason_summary,
                "gate_severity": gate_severity,
                "routing_followed": ("gate" if gate_has_authority and gate_severity
                                     else "reported"),
                "agreement": ledger.get("agreement"),
                "disagreement_type": ledger.get("disagreement_type"),
                "ledger": ledger.get("status"),
                "latency_ms": decision.latency_ms,
            }
        except Exception as exc:  # noqa: BLE001 - shadow must never break dispatch
            log.warning("shadow ledger write failed for tenant %s: %s", tenant_id, exc)
            shadow = {"error": f"{type(exc).__name__}: {exc}"}
    elif gate_error is not None:
        shadow = {"error": gate_error}

    response = {
        "status": "routed_with_anecdote",
        "tenant_id": tenant_id,
        "reported_severity": reported_severity,
        "routing_severity": routing_severity,
        "anecdote_id": anecdote["anecdote_id"],
        "headline": anecdote["headline"],
        "dispatched_to": results
    }
    if shadow is not None:
        response["shadow_decision"] = shadow
    if decision is not None:
        response["provenance"] = {
            "decision_id": decision.decision_id,
            "model_version": decision.model_version,
            "questions_version": decision.questions_version,
            "reason_code": decision.reason_code,
            "routing_followed": (
                "gate" if gate_has_authority and gate_severity else "reported"
            ),
        }
    if gate_has_authority:
        response["decision_unavailable"] = bool(
            decision is not None and decision.decision_unavailable
        )
        response["enforce"] = {
            "action": action if fired else "none",
            "side_effect": enforce_side_effect,
            "authorization": auth,
        }
    return response


def _policy_grants_authority(policy: dict) -> bool:
    """Authority is enabled, enforce, and no shadow flag. Same rule as main()."""
    gate = (policy or {}).get("decision_gate") or {}
    return (
        bool(gate.get("enabled"))
        and not bool(gate.get("shadow"))
        and gate.get("mode") == "enforce"
    )


def primary_input(event_payload: dict, **kwargs) -> dict:
    """task 186: lidless is the primary input. The gate is the authority.

    main() keeps today's path: a tenant that has not granted authority still
    restarts on a reported critical. This entry does not. No authority means
    withhold — no dispatch, no restart, no invented provenance. Shipped
    tenant files are not flipped here.
    """
    tenant_id = event_payload.get("tenant_id", "global")
    policy = load_tenant_policy(tenant_id)
    if not _policy_grants_authority(policy):
        return {
            "status": "withheld",
            "tenant_id": tenant_id,
            "reason": "gate_not_authority",
            "dispatched_to": [],
        }
    return main(event_payload, **kwargs)
