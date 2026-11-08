# ==============================================================================
# Windmill Flow: decision_gate (judgment in, typed decision out)
# ==============================================================================
# L-005 (+ 2026-09-20 restructure). Asks Jev the versioned gate questions about a
# derived state, applies the tenant's declared thresholds, and returns a Decision.
# It performs NO side effects: no dispatch, no restart, no ledger write, no
# notification. The caller decides what to do with the answer.
#
# Two invariants, both tested:
#
#   FAIL CLOSED.  No exception path can produce an action. Every error class
#                 returns action="none" with decision_unavailable=True.
#   POLICY WINS.  The action always comes from the tenant policy. The model's
#                 severity can only *withhold* it, never grant it.
#
# The explanation is STRUCTURED, not prose:
#   reason_code       - one value from REASON_CODES, cheap to count and filter
#   failed_checks     - [{check, actual, threshold, comparison}, ...]
#   reason_summary    - human-readable, BOUNDED to MAX_REASON_SUMMARY chars
#   reason_detail_ref - pointer to the archived full text, set only when the
#                       summary had to be truncated
#
# The ledger stores the full record (Decision.to_dict()); the event envelope and
# the audit line carry Decision.envelope(), which omits `reason_detail_full`.
# That is the "pointer to prose if absolutely necessary" rule: the prose lives
# under a key, not in the event.
#
# Answers are read by duck-typing (choice / score / noul attributes), so this
# module does not import the SDK's answer classes and can be tested with a stub.
# The hourly budget check is authorize_remediation() — pure, no side effects.
# Firing the restart is the router's job (L-009). evaluate() does not count
# restarts, so this module stays a pure judgment function.

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from windmill.flows.decision_state import derive_state, state_fingerprint

DEFAULT_STATE_MODE = None  # never defaulted: policy must declare it (ADR-0001)
DEFAULT_QUESTIONS_VERSION = "v1"
ACTIONABLE_SEVERITIES = ("critical",)
MAX_REASON_SUMMARY = 200

REASON_CODES = (
    # configuration / policy states (no judgment was needed or possible)
    "gate_disabled",
    "remediation_not_enabled_by_policy",
    "no_thresholds_declared",
    "state_mode_not_declared",
    # judgments
    "thresholds_satisfied",
    "threshold_failed",
    "severity_not_permitted",
    "threshold_failed_and_severity_not_permitted",
    # shadow: evaluated, recorded, structurally incapable of acting
    "shadow_mode",
    # service failures (always fail closed)
    "gate_timeout",
    "gate_connection_error",
    "gate_rate_limited",
    "gate_authentication_error",
    "gate_bad_request",
    "gate_error",
    # malformed answers
    "missing_answer",
    "state_error",
)


class GateError(RuntimeError):
    """Raised only for configuration errors the caller must fix, never for a decision."""


@dataclass
class Decision:
    """A proposal. `action` is "none" unless every threshold and policy check passed."""

    action: str = "none"
    severity: Optional[str] = None
    # OD-3 (lidless#3): the severity the detector reported, kept alongside the gate's own
    # judgement so the divergence between them stays visible instead of being overwritten.
    reported_severity: Optional[str] = None
    fault_class: Optional[str] = None
    severity_confidence: Optional[float] = None
    class_confidence: Optional[float] = None
    raw_answers: dict = field(default_factory=dict)
    thresholds_applied: dict = field(default_factory=dict)
    failed_checks: list = field(default_factory=list)
    model_version: Optional[str] = None
    questions_version: Optional[str] = None
    state_mode: Optional[str] = None
    policy_source: Optional[str] = None
    evaluated: bool = False
    escalated: bool = False
    decision_unavailable: bool = False
    # Shadow runs record the verdict they would have reached, then act on nothing.
    would_be_action: Optional[str] = None
    would_be_code: Optional[str] = None
    cache_hit: bool = False
    reason_code: str = ""
    reason_summary: str = ""
    reason_detail_ref: Optional[str] = None
    reason_detail_full: Optional[str] = None   # persisted, never shipped
    decision_id: Optional[str] = None
    state_fingerprint: Optional[str] = None
    latency_ms: Optional[int] = None
    usage: dict = field(default_factory=dict)
    # Copied off a service error when the error carried one. Never invented.
    request_id: Optional[str] = None

    def to_dict(self) -> dict:
        """Complete record, for the append-only ledger."""
        return asdict(self)

    def envelope(self) -> dict:
        """What goes on the wire: no unbounded prose, only the pointer to it."""
        record = asdict(self)
        record.pop("reason_detail_full", None)
        return record


# ------------------------------------------------------------------ helpers

def _read_answer(answer: Any) -> dict:
    """Normalise one SDK answer into a plain dict, by attribute presence.

    A Noul answer carries only `noul` - there is no confidence field, and none is
    invented here.
    """
    out: dict = {}
    for key in ("choice", "score", "noul"):
        if hasattr(answer, key):
            out[key] = getattr(answer, key)
    if hasattr(answer, "confidence"):
        out["confidence"] = getattr(answer, "confidence")
    if hasattr(answer, "probabilities"):
        out["probabilities"] = dict(getattr(answer, "probabilities") or {})
    if hasattr(answer, "legend"):
        out["legend"] = dict(getattr(answer, "legend") or {})
    return out


def _gate_policy(tenant_policy: dict) -> dict:
    return (tenant_policy or {}).get("decision_gate", {}) or {}


def _model_for(policy: dict) -> str:
    gate = _gate_policy(policy)
    model = gate.get("model")
    if not model or str(model).endswith("-latest"):
        raise GateError(
            "decision_gate.model must pin a version (e.g. 'jev-1.13.0'); an alias "
            "invalidates calibrated thresholds"
        )
    return str(model)


def _decision_id(fingerprint: str, thresholds: dict, model: str, version: str,
                 window: Optional[int]) -> str:
    """Stable for identical state, thresholds, model and window; otherwise unique."""
    payload = json.dumps(
        {"fp": fingerprint, "t": thresholds, "m": model, "v": version, "w": window},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _check(name: str, actual, threshold, comparison: str) -> dict:
    return {"check": name, "actual": actual, "threshold": threshold, "comparison": comparison}


def _content_address(text: str) -> str:
    """The ledger reference is the digest of what it points at, not a synthetic id.

    A decision record is evidence, and the identifier contract requires evidence to be
    content-addressed. Storing the full explanation under its own digest means a reader can verify
    the reference without trusting the ledger's row numbering, and the reference survives the
    ledger being rebuilt — which a pointer into that numbering would not.
    """
    return "artifact://sha256/" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _explain(decision: Decision, code: str, checks: Optional[list] = None,
             detail: str = "") -> Decision:
    """Attach a code, structured checks, and a bounded summary.

    The summary is always within MAX_REASON_SUMMARY. Anything longer is set on
    `reason_detail_full` (for the ledger) and referenced by content address on
    `reason_detail_ref`, so the event envelope stays bounded and the record stays evidence.
    """
    if code not in REASON_CODES:
        raise GateError(f"unknown reason code {code!r}; add it to REASON_CODES")

    checks = checks or []
    decision.reason_code = code
    decision.failed_checks = checks
    prose = detail or "; ".join(
        f"{c['check']} {c['actual']} {c['comparison']} {c['threshold']}" for c in checks
    )
    summary = f"{code}: {prose}" if prose else code
    if len(summary) > MAX_REASON_SUMMARY:
        decision.reason_detail_full = summary
        decision.reason_detail_ref = _content_address(summary)
        decision.reason_summary = summary[: MAX_REASON_SUMMARY - 3] + "..."
    else:
        decision.reason_summary = summary
    return decision


def _decline(decision: Decision, code: str, *, unavailable: bool = False,
             evaluated: bool = True, detail: str = "",
             checks: Optional[list] = None) -> Decision:
    """Every early exit funnels through here, so no path can produce an action."""
    decision.action = "none"
    decision.evaluated = evaluated
    decision.decision_unavailable = unavailable
    _explain(decision, code, checks=checks, detail=detail)
    decision.escalated = code in ("threshold_failed", "severity_not_permitted",
                                  "threshold_failed_and_severity_not_permitted") or unavailable
    return decision


# ------------------------------------------------------------------ the gate

def _judge(event_payload: dict, tenant_policy: dict, *, client: Any = None,
           window: Optional[int] = None, schema: Optional[dict] = None,
           _shadow: bool = False, cache: Optional[Any] = None) -> Decision:
    """Judge one event and return a Decision. No side effects, ever.

    client: anything exposing system_one(state=..., questions=..., model=...).
            Injected for tests; when omitted, a TypeSafeClient is constructed.
    window: optional time bucket (e.g. int(time.time() // 60)) mixed into the
            decision id. Without it a fingerprint cannot separate two identical
            structured states - see the note in tests/test_derive_state.py.
    """
    started = time.perf_counter()
    policy = tenant_policy or {}
    gate = _gate_policy(policy)
    decision = Decision(policy_source=policy.get("tenant_id"))

    if not gate.get("enabled", False) and not _shadow:
        return _decline(decision, "gate_disabled", evaluated=False)

    # Thresholds are per action, never one global number.
    auto = policy.get("auto_remediation", {}) or {}
    action = auto.get("target_action", "none")
    remediation_permitted = bool(auto.get("enabled", False)) and action not in (None, "none")
    thresholds = (gate.get("thresholds") or {}).get(action)
    if not isinstance(thresholds, dict):
        thresholds = {}

    config_block = None
    if not remediation_permitted:
        config_block = "remediation_not_enabled_by_policy"
    elif not thresholds:
        config_block = "no_thresholds_declared"

    if config_block and not _shadow:
        return _decline(decision, config_block,
                        detail=f"{config_block} (target_action={action})")
    # In shadow, a configuration block is NOT a reason to skip the judgment: the
    # point of a shadow period is evidence about the gate's answers, and a tenant
    # that never auto-remediates is precisely the one whose thresholds cannot yet
    # be justified from data. We evaluate, record the answers, and permit nothing.

    state_mode = gate.get("state_mode", DEFAULT_STATE_MODE)
    if state_mode is None:
        return _decline(decision, "state_mode_not_declared")

    model = _model_for(policy)  # raises GateError: a config error, not a decision
    questions_version = gate.get("questions_version", DEFAULT_QUESTIONS_VERSION)

    try:
        state = derive_state(event_payload, state_mode)
    except Exception as exc:  # noqa: BLE001 - a bad envelope must not decide anything
        return _decline(decision, "state_error", unavailable=True,
                        detail=f"{type(exc).__name__}: {exc}"[:MAX_REASON_SUMMARY])

    decision.state_mode = state_mode
    decision.thresholds_applied = dict(thresholds)
    decision.questions_version = questions_version
    fp = state_fingerprint(state)
    decision.state_fingerprint = fp
    decision.decision_id = _decision_id(fp, thresholds, model, questions_version, window)

    tenant_id = policy.get("tenant_id") or event_payload.get("tenant_id", "global")
    container = event_payload.get("host_meta", {}).get("container", "workload")
    if cache is not None:
        cached = cache.get(tenant_id, container, fp)
        if cached is not None:
            return cached

    if schema is None:
        from windmill.flows.jev import loader
        schema = loader.load_schema(build=True)
    questions = schema.get("questions")
    decision.questions_version = schema.get("version", questions_version)

    # ---- the call -------------------------------------------------------
    try:
        if client is None:
            from typesafe_sdk import TypeSafeClient
            client = TypeSafeClient()  # reads TYPESAFE_API_KEY from the environment
        t0 = time.perf_counter()
        response = client.system_one(state=state, questions=questions, model=model)
        decision.latency_ms = round((time.perf_counter() - t0) * 1000)
    except Exception as exc:  # noqa: BLE001 - every failure mode fails closed
        name = type(exc).__name__
        if "Timeout" in name:
            code = "gate_timeout"
        elif "Connection" in name:
            code = "gate_connection_error"
        elif "RateLimit" in name:
            code = "gate_rate_limited"
        elif "Authentication" in name:
            code = "gate_authentication_error"
        elif "BadRequest" in name:
            code = "gate_bad_request"
        else:
            code = "gate_error"
        declined = _decline(decision, code, unavailable=True,
                            detail=f"{name}: {str(exc)[:120]}")
        rid = getattr(exc, "request_id", None)
        if rid is None:
            rid = getattr(exc, "requestId", None)
        declined.request_id = rid
        return declined

    # ---- read the answers ----------------------------------------------
    answers = getattr(response, "answers", {}) or {}
    decision.model_version = getattr(response, "model", None) or model
    usage = getattr(response, "usage", None)
    decision.usage = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }
    decision.raw_answers = {qid: _read_answer(a) for qid, a in answers.items()}

    required = ("fault_class", "severity", "restart_would_help", "restart_discards_state")
    missing = [q for q in required if q not in answers]
    if missing:
        return _decline(decision, "missing_answer", unavailable=True,
                        detail=f"missing_answer:{','.join(missing)}")

    decision.fault_class = decision.raw_answers["fault_class"].get("choice")
    decision.class_confidence = decision.raw_answers["fault_class"].get("confidence")
    decision.severity = decision.raw_answers["severity"].get("choice")
    decision.severity_confidence = decision.raw_answers["severity"].get("confidence")
    help_noul = decision.raw_answers["restart_would_help"].get("noul")
    discards_noul = decision.raw_answers["restart_discards_state"].get("noul")

    # ---- shadow with a configuration block: record, then permit nothing ------
    if config_block:
        decision.evaluated = True
        return _decline(decision, config_block,
                        detail=f"{config_block} (target_action={action}); "
                               f"judgment recorded in shadow for evidence only")

    # ---- apply thresholds (each one can only withhold) -------------------
    failed: list = []
    if thresholds.get("min_severity_confidence") is not None:
        got = decision.severity_confidence or 0.0
        if got < thresholds["min_severity_confidence"]:
            failed.append(_check("min_severity_confidence", round(got, 4),
                                 thresholds["min_severity_confidence"], "lt"))
    if thresholds.get("min_restart_would_help") is not None:
        if help_noul is None or help_noul < thresholds["min_restart_would_help"]:
            failed.append(_check("min_restart_would_help", help_noul,
                                 thresholds["min_restart_would_help"], "lt"))
    if thresholds.get("max_restart_discards_state") is not None:
        if discards_noul is None or discards_noul > thresholds["max_restart_discards_state"]:
            failed.append(_check("max_restart_discards_state", discards_noul,
                                 thresholds["max_restart_discards_state"], "gt"))
    if thresholds.get("min_is_transient") is not None and "is_transient" in answers:
        transient = decision.raw_answers["is_transient"].get("noul")
        if transient is not None and transient < thresholds["min_is_transient"]:
            failed.append(_check("min_is_transient", transient,
                                 thresholds["min_is_transient"], "lt"))

    permitted = gate.get("allowed_severities", list(ACTIONABLE_SEVERITIES))
    severity_blocked = decision.severity not in permitted
    if severity_blocked:
        failed.append(_check("allowed_severities", decision.severity,
                             list(permitted), "not_in"))

    decision.evaluated = True

    if failed:
        if severity_blocked and len(failed) == 1:
            code = "severity_not_permitted"
        elif severity_blocked:
            code = "threshold_failed_and_severity_not_permitted"
        else:
            code = "threshold_failed"
        mode = gate.get("on_low_confidence", "escalate")
        decision = _decline(decision, code, checks=failed)
        decision.escalated = mode == "escalate"
        if mode == "route_only":
            decision.reason_summary = f"route_only: {decision.reason_summary}"[:MAX_REASON_SUMMARY]
        if cache is not None:
            cache.put(tenant_id, container, fp, decision)
        return decision

    decision.action = action
    _explain(decision, "thresholds_satisfied",
             detail=f"thresholds_satisfied for {action}")
    decision.latency_ms = decision.latency_ms if decision.latency_ms is not None else \
        round((time.perf_counter() - started) * 1000)
    if cache is not None:
        cache.put(tenant_id, container, fp, decision)
    return decision


def _apply_shadow(decision: Decision, shadow: bool) -> Decision:
    """Shadow never acts. The verdict is preserved, the authority is removed.

    This is structural, not a convention: whatever the judgment was, the action
    returned to the caller is "none" and escalation is off. `would_be_action`
    and `would_be_code` are what the gate would have done with authority.
    """
    if not shadow:
        return decision
    decision.would_be_action = decision.action
    decision.would_be_code = decision.reason_code
    decision.action = "none"
    decision.escalated = False
    _explain(decision, "shadow_mode", checks=decision.failed_checks,
             detail=f"would_be={decision.would_be_action} ({decision.would_be_code}); "
                    f"{decision.reason_summary}")
    return decision


def evaluate(event_payload: dict, tenant_policy: dict, *, client: Any = None,
             window: Optional[int] = None, schema: Optional[dict] = None,
             shadow: bool = False, cache: Optional[Any] = None) -> Decision:
    """Judge one event, optionally in shadow, and return a Decision.

    shadow=True evaluates even when the tenant's gate is disabled, records the
    verdict on `would_be_action`, and returns action="none" unconditionally -
    that is what makes shadow safe to run on live traffic (L-007).
    """
    decision = _judge(event_payload, tenant_policy, client=client, window=window,
                      schema=schema, _shadow=shadow, cache=cache)
    return _apply_shadow(decision, shadow)


def evaluate_batch(events: list, policies: dict, **kwargs) -> list:
    """Convenience for tests and dry runs: one decision per (event, tenant policy)."""
    return [evaluate(event, policies.get(event.get("tenant_id"), {}), **kwargs)
            for event in events]


def authorize_remediation(decision, policy, *, reported_severity, restarts_this_hour,
                          cache: Optional[Any] = None, container: Optional[str] = None):
    """Whether a restart may fire. Pure. The router calls this and checks again.

    A missing cap is not unlimited. A bool is not an int. A negative count is
    not a remaining budget. The reported severity must already be critical:
    this function withholds, it does not grant a restart today would not take.
    """
    auto = (policy or {}).get("auto_remediation") or {}
    action = auto.get("target_action") or "none"
    permitted = bool(auto.get("enabled", False)) and action not in (None, "none", "")
    cap = auto.get("max_restarts_per_hour")
    count_ok = (
        isinstance(restarts_this_hour, int)
        and not isinstance(restarts_this_hour, bool)
        and restarts_this_hour >= 0
    )
    cap_ok = isinstance(cap, int) and not isinstance(cap, bool) and cap >= 0
    budget_ok = count_ok and cap_ok and restarts_this_hour < cap
    gate_ok = (
        decision is not None
        and getattr(decision, "action", None) == action
        and getattr(decision, "reason_code", None) == "thresholds_satisfied"
        and not getattr(decision, "decision_unavailable", False)
    )
    reported_ok = reported_severity == "critical"

    tenant_id = (policy or {}).get("tenant_id") or "global"
    fault_class = getattr(decision, "fault_class", None)
    hysteresis_suppressed = False
    if cache is not None and container and fault_class:
        hysteresis_suppressed = cache.is_action_suppressed_by_hysteresis(
            tenant_id, container, fault_class
        )

    if not permitted:
        reason = "policy_withheld"
    elif not reported_ok:
        reason = "reported_severity_not_critical"
    elif not gate_ok:
        reason = "gate_withheld"
    elif hysteresis_suppressed:
        reason = "hysteresis_suppressed"
    elif not budget_ok:
        reason = "budget_exhausted"
    else:
        reason = "authorized"
    fire = reason == "authorized"
    return {
        "fire": fire,
        "action": action if fire else "none",
        "reason": reason,
        "policy_permitted": permitted,
        "budget_ok": budget_ok,
        "gate_ok": gate_ok,
        "reported_ok": reported_ok,
        "hysteresis_suppressed": hysteresis_suppressed,
        "restarts_this_hour": restarts_this_hour,
        "max_restarts_per_hour": cap,
    }
