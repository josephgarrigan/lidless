# ==============================================================================
# Windmill Flow: shadow_ledger (decisions recorded, nothing acted on)
# ==============================================================================
# L-007. One JSON line per gated event: what the gate WOULD have done, what the
# current rule DID do, and whether they agree. This is the evidence L-008 derives
# thresholds from, so the format is deliberately boring and append-only.
#
# Three rules, all tested:
#
#   1. DECISION METADATA ONLY. A fixed allow-list of fields is written. Log
#      content, `raw_message`, and the sanitized preview are never written - the
#      ledger must be readable without re-reading a tenant's logs. The one
#      exception is `reason_detail_full`, which is the prose the bounded
#      `reason_summary` truncated, and it belongs to the same tenant's archive.
#   2. NEVER BLOCKS THE PIPELINE. Every failure path returns a status dict; this
#      module does not raise into a dispatch path. A full disk must not drop an
#      incident notification.
#   3. ARCHIVE RESOLUTION IS EXPLICIT. A tenant's own `s3_archive_prefix` wins;
#      otherwise the platform tenant's, marked as a fallback (OD-4 ruling,
#      2026-09-20). The s3:// prefix maps to a local root so this is testable
#      and so the deploy decides the mount.
#
# Stages (PLAN.md 2.7.1): this is stage 1 - files in a folder. Tags are stage 2,
# a Matrix room is stage 3. Do not build stage 2 here.

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LEDGER_ROOT = "/var/lib/lidless/shadow"
MAX_LINE_CHARS = 4000

# The allow-list. Anything not named here is not written, which is what makes
# "no log content" a property of the format rather than a promise.
LEDGER_FIELDS = (
    "ts", "tenant_id", "organization", "container", "state_mode",
    "decision_id", "state_fingerprint", "model_version", "questions_version",
    "reason_code", "reason_summary", "reason_detail_ref", "failed_checks",
    "thresholds_applied", "would_be_action", "would_be_code",
    "evaluated", "escalated", "decision_unavailable",
    "severity", "fault_class", "severity_confidence", "class_confidence",
    "answers", "latency_ms", "usage", "rule_outcome", "agreement",
    "disagreement_type", "policy_source", "archive_source",
)

_S3 = re.compile(r"^s3://([^/]+)/?(.*)$")


def ledger_root() -> Path:
    return Path(os.environ.get("SHADOW_LEDGER_ROOT", DEFAULT_LEDGER_ROOT))


def archive_for(tenant_policy: dict, platform_policy: dict | None = None) -> tuple:
    """Return (prefix, source) where source is 'tenant' or 'platform_fallback'."""
    prefix = ((tenant_policy or {}).get("routing") or {}).get("s3_archive_prefix")
    if prefix:
        return prefix, "tenant"
    fallback = ((platform_policy or {}).get("routing") or {}).get("s3_archive_prefix")
    if fallback:
        return fallback, "platform_fallback"
    return "s3://unassigned/shadow/", "unassigned"


def archive_dir(prefix: str, root: Path | None = None) -> Path:
    """Map an s3:// prefix onto a local directory under the ledger root."""
    root = root or ledger_root()
    match = _S3.match(prefix or "")
    if not match:
        return root / "unassigned"
    bucket, path = match.group(1), match.group(2).strip("/")
    return root / bucket / path if path else root / bucket


def rule_outcome_from(dispatched_to: list) -> dict:
    """Summarise what the current rule did, from the router's own result list."""
    dispatched = list(dispatched_to or [])
    remediation = [d for d in dispatched if d.startswith("triggered_remediation_")]
    return {
        "dispatched_to": dispatched,
        "remediation_triggered": bool(remediation),
        "remediation_actions": remediation,
    }


def agreement_for(decision, rule_outcome: dict) -> tuple:
    """Compare the gate's withheld verdict against what the rule actually did."""
    rule_acted = bool(rule_outcome.get("remediation_triggered"))
    gate_would = (decision.would_be_action or "none") not in (None, "none")
    agreement = rule_acted == gate_would
    if agreement:
        return True, None
    return False, "gate_withholds" if rule_acted else "gate_would_act"


def build_line(decision, tenant_policy: dict, rule_outcome: dict, *,
               platform_policy: dict | None = None,
               event_payload: dict | None = None,
               now: str | None = None) -> dict:
    """Build one ledger record, containing only allow-listed fields."""
    prefix, source = archive_for(tenant_policy, platform_policy)
    agreement, disagreement = agreement_for(decision, rule_outcome)
    detail = decision.to_dict()
    host_meta = (event_payload or {}).get("host_meta") or {}

    record = {
        "ts": now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tenant_id": tenant_policy.get("tenant_id"),
        "organization": tenant_policy.get("organization"),
        # Structured metadata from the envelope, never from the message text.
        "container": host_meta.get("container"),
        "archive_source": source,
        "policy_source": detail.get("policy_source"),
        "state_mode": detail.get("state_mode"),
        "decision_id": detail.get("decision_id"),
        "state_fingerprint": detail.get("state_fingerprint"),
        "model_version": detail.get("model_version"),
        "questions_version": detail.get("questions_version"),
        "reason_code": detail.get("reason_code"),
        "reason_summary": detail.get("reason_summary"),
        "reason_detail_ref": detail.get("reason_detail_ref"),
        "failed_checks": detail.get("failed_checks"),
        "thresholds_applied": detail.get("thresholds_applied"),
        "would_be_action": detail.get("would_be_action"),
        "would_be_code": detail.get("would_be_code"),
        "evaluated": detail.get("evaluated"),
        "escalated": detail.get("escalated"),
        "decision_unavailable": detail.get("decision_unavailable"),
        "severity": detail.get("severity"),
        "fault_class": detail.get("fault_class"),
        "severity_confidence": detail.get("severity_confidence"),
        "class_confidence": detail.get("class_confidence"),
        "answers": detail.get("raw_answers"),
        "latency_ms": detail.get("latency_ms"),
        "usage": detail.get("usage"),
        "rule_outcome": rule_outcome,
        "agreement": agreement,
        "disagreement_type": disagreement,
    }

    # Only the tenant's own archive gets the truncated prose; the event envelope
    # and anything cross-tenant never does.
    if source == "tenant" and detail.get("reason_detail_full"):
        record["reason_detail_full"] = detail["reason_detail_full"]

    return {k: v for k, v in record.items() if k in LEDGER_FIELDS or k == "reason_detail_full"}


def matrix_line(record: dict) -> str:
    """Sanitized one-line summary for the platform room. No log content, bounded."""
    rule = record.get("rule_outcome") or {}
    text = (f"[shadow] tenant={record.get('tenant_id')} container={record.get('container')} "
            f"rule={'acted' if rule.get('remediation_triggered') else 'idle'} "
            f"gate={record.get('would_be_action') or 'none'} "
            f"agree={record.get('agreement')} code={record.get('reason_code')}")
    return text[:200]


def notify_matrix(text: str, room_id: str | None, *, post=None) -> dict:
    """Best-effort notification. Never raises."""
    if not room_id:
        return {"status": "skipped", "why": "no room configured"}
    try:
        if post is None:
            import requests  # imported lazily: the ledger must work without it
            base = os.environ.get("SYNAPSE_HOMESERVER_URL", "")
            token = os.environ.get("GATUS_MATRIX_TOKEN", "")
            if not base or not token:
                return {"status": "skipped", "why": "SYNAPSE_HOMESERVER_URL/GATUS_MATRIX_TOKEN unset"}
            url = (f"{base}/_matrix/client/r0/rooms/{room_id}/send/m.room.message"
                   f"?access_token={token}")
            post = lambda u, body: requests.post(u, json=body, timeout=5)  # noqa: E731
            post(url, {"msgtype": "m.text", "body": text})
        else:
            post(None, {"msgtype": "m.text", "body": text})
        return {"status": "sent"}
    except Exception as exc:  # noqa: BLE001 - non-blocking by contract
        return {"status": "error", "error": type(exc).__name__}


def record(decision, tenant_policy: dict, rule_outcome: dict, *,
           platform_policy: dict | None = None, root: Path | None = None,
           event_payload: dict | None = None,
           room_id: str | None = None, notify: bool = False, post=None,
           now: str | None = None) -> dict:
    """Append one ledger line. Returns a status dict and never raises."""
    try:
        line = build_line(decision, tenant_policy, rule_outcome,
                          platform_policy=platform_policy, event_payload=event_payload,
                          now=now)
        prefix, source = archive_for(tenant_policy, platform_policy)
        directory = archive_dir(prefix, root)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"shadow-{(line['ts'] or '')[:10]}.jsonl"

        encoded = json.dumps(line, separators=(",", ":"), sort_keys=True)
        if len(encoded) > MAX_LINE_CHARS:
            line.pop("answers", None)
            line["answers_omitted"] = "line exceeded MAX_LINE_CHARS"
            encoded = json.dumps(line, separators=(",", ":"), sort_keys=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")

        status = {"status": "recorded", "path": str(path), "archive_source": source,
                  "agreement": line["agreement"], "disagreement_type": line["disagreement_type"],
                  "bytes": len(encoded)}
        if notify:
            status["notify"] = notify_matrix(matrix_line(line), room_id, post=post)
        return status
    except Exception as exc:  # noqa: BLE001 - a ledger failure must not stop dispatch
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
