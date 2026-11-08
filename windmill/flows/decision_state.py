# ==============================================================================
# Decision state: what the decision gate is allowed to transmit
# ==============================================================================
# L-003. The gate decides on exactly what it is handed, so this module is the
# single place that turns a raw Vector event envelope into gate state - and the
# single place that enforces the payload boundary (ADR-0001).
#
# Two rules are load-bearing:
#
#   1. Sanitize BEFORE any egress. `sanitize_message()` lives here now, not inside
#      `anecdote_engine.synthesize_anecdote()` where it ran after pattern matching.
#      Nothing in this module sends anything; it only produces the state that a
#      caller may send.
#   2. The mode is explicit. `derive_state()` has no default mode, so a caller
#      cannot silently select the wider one.
#
# Modes:
#   structured_only   - no log text at all. The gate can judge severity and
#                       transience but cannot resolve fine-grained fault class.
#   sanitized_content - adds `sanitized_preview`, the same redacted string
#                       lidless already posts to Slack/Matrix/PagerDuty.
#
# Pure functions only: no I/O, no network, no mutation of the input.

import re

STATE_MODES = ("structured_only", "sanitized_content")
MAX_PREVIEW_CHARS = 250

_IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_CREDENTIAL = re.compile(r"(Bearer|token|secret|password|key)[=:\s]+[a-zA-Z0-9_\-\.]+", re.IGNORECASE)
_PATH = re.compile(r"(/[a-zA-Z0-9_\-]+)+/([a-zA-Z0-9_\-\.]+)")


class StateModeError(ValueError):
    """Raised when a caller asks for a state mode that does not exist."""


def sanitize_message(raw_msg: str) -> str:
    """Strip internal IPs, credentials, and path prefixes from a log line.

    Moved here verbatim from `anecdote_engine` so the gate and the anecdote share
    one implementation. `anecdote_engine` re-exports it for backwards compatibility.
    """
    if not raw_msg:
        return ""
    cleaned = _IPV4.sub("[internal-ip]", raw_msg)
    cleaned = _CREDENTIAL.sub(r"\1=[REDACTED]", cleaned)
    cleaned = _PATH.sub(r"\2", cleaned)
    return cleaned.strip()


def derive_state(event_payload: dict, mode: str) -> dict:
    """Build the state object a decision gate is permitted to transmit.

    `mode` is required - pass "structured_only" or "sanitized_content". Anything
    else raises StateModeError rather than falling back to a default.

    The returned dict carries only fields that already exist in the envelope. No
    field is inferred, and `raw_message` is never copied out of `event_meta`.
    """
    if mode not in STATE_MODES:
        raise StateModeError(
            f"unknown state mode {mode!r}; expected one of {', '.join(STATE_MODES)}"
        )

    event_meta = event_payload.get("event_meta", {}) or {}
    host_meta = event_payload.get("host_meta", {}) or {}
    tenant = event_payload.get("tenant", {}) or {}

    state = {
        "tenant_id": event_payload.get("tenant_id"),
        "organization": event_payload.get("org_id"),
        "environment": tenant.get("environment"),
        "is_error": bool(event_meta.get("is_error", event_payload.get("is_error", False))),
        "reported_severity": event_payload.get("severity"),
        "status_code": event_meta.get("status_code"),
        "stream": event_meta.get("stream"),
        "timestamp": event_meta.get("timestamp"),
        "container": host_meta.get("container"),
        "hostname": host_meta.get("hostname"),
        "state_mode": mode,
    }

    if mode == "sanitized_content":
        state["sanitized_preview"] = sanitize_message(
            event_meta.get("raw_message", "")
        )[:MAX_PREVIEW_CHARS]

    return state


def state_fingerprint(state: dict) -> str:
    """Stable fingerprint of gate state, for decision caching and the shadow ledger.

    Excludes nothing and normalises ordering, so two identical events inside the
    cache window resolve to one decision (L-011).
    """
    import hashlib
    import json

    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
