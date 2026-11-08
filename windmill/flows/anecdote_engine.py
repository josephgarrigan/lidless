# ==============================================================================
# Windmill Flow: anecdote_engine (Runtime Narrative & Anecdote Synthesizer)
# ==============================================================================
# Translates raw cryptic telemetry, panics, and 5xx logs into structured,
# user-friendly anecdotes, root-cause narratives, and executive summaries.

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, List, Optional

ERROR_PATTERNS = [
    {
        "id": "DB_CONNECTION_EXHAUSTION",
        "regex": r"(?i)(too many connections|connection refused|pool exhausted|could not connect to server|connection timeout|remaining connection slots|deadlock detected|connection reset by peer)",
        "headline": "Database Connection Congestion",
        "story_template": "At {time_str}, {container} faced temporary database connection pool starvation. Upstream queries queued momentarily before failing gracefully.",
        "user_impact": "Transient latency spike or temporary 503 error on active user queries.",
        "recommendation": "Connection pools are automatically recycling. No data was corrupted.",
        "severity": "HIGH",
        "action_required": False
    },
    {
        "id": "OOM_MEMORY_CEILING",
        "regex": r"(?i)(out of memory|oom|killed process|cannot allocate memory|memory cgroup out of memory|fatal error: runtime: out of memory)",
        "headline": "Workload Memory Ceiling Reached",
        "story_template": "At {time_str}, {container} exceeded its designated memory threshold while handling high throughput and was recycled by the host supervisor.",
        "user_impact": "Brief service interruption (< 5 seconds) while the workload spun back up.",
        "recommendation": "Container successfully restarted. Caches are warming up.",
        "severity": "CRITICAL",
        "action_required": False
    },
    {
        "id": "AUTH_SECURITY_REJECTION",
        "regex": r"(?i)(jwt expired|token signature invalid|unauthorized|401 unauthorized|permission denied|access_denied|tls handshake error|bad certificate|forbidden|403 forbidden)",
        "headline": "Authentication / Security Handshake Rejection",
        "story_template": "At {time_str}, an inbound request to {container} was rejected due to an expired/invalid cryptographic session token or TLS certificate mismatch.",
        "user_impact": "The client was prompted to re-authenticate or refresh their credentials.",
        "recommendation": "Inspect client auth token expiry settings or TLS trust stores.",
        "severity": "MEDIUM",
        "action_required": True
    },
    {
        "id": "UPSTREAM_TIMEOUT",
        "regex": r"(?i)(gateway timeout|504 gateway|502 bad gateway|upstream request timeout|deadline exceeded|timed out|context deadline exceeded)",
        "headline": "Upstream Dependency Delay",
        "story_template": "At {time_str}, {container} experienced a delay waiting on an external dependency or microservice API response.",
        "user_impact": "Client requests took longer than standard SLA or encountered a 504 timeout.",
        "recommendation": "Downstream retries are actively managing the backlog.",
        "severity": "MEDIUM",
        "action_required": False
    },
    {
        "id": "RUNTIME_PANIC",
        "regex": r"(?i)(panic:|fatal error:|segmentation fault|segfault|nullpointerexception|uncaught exception|core dumped)",
        "headline": "Unhandled Application Exception",
        "story_template": "At {time_str}, {container} encountered an unexpected runtime fault resulting in an unhandled exception.",
        "user_impact": "Specific transaction could not be completed.",
        "recommendation": "Error payload has been captured for engineers with Dozzle log drill-down.",
        "severity": "HIGH",
        "action_required": True
    },
    {
        "id": "BRIDGE_SYNC_FAULT",
        "regex": r"(?i)(lens_class=bridge_error|bridge=\w+|matrix-appservice|mautrix-|bridge sync failed|appservice.*(?:rejected|timeout|failed)|matrix.*transaction.*failed)",
        "headline": "Cross-Network Bridge Synchronization Fault",
        "story_template": "At {time_str}, {container} experienced a message exchange or synchronization failure across federated bridge infrastructure.",
        "user_impact": "Cross-network messages (ActivityPub, Discord, iMessage, or Buzz) were delayed or rejected.",
        "recommendation": "Inspect homeserver appservice registration tokens and bridge network state.",
        "severity": "HIGH",
        "action_required": True
    }
]

FAULT_CLASS_TEMPLATES = {
    "db_connection_exhaustion": "DB_CONNECTION_EXHAUSTION",
    "oom_memory_ceiling": "OOM_MEMORY_CEILING",
    "auth_security_rejection": "AUTH_SECURITY_REJECTION",
    "upstream_timeout": "UPSTREAM_TIMEOUT",
    "runtime_panic": "RUNTIME_PANIC",
    "bridge_sync_fault": "BRIDGE_SYNC_FAULT",
}

UNCLASSIFIED_PATTERN = {
    "id": "UNCLASSIFIED_RUNTIME_FAULT",
    "headline": "Unrecognized Runtime Fault",
    "story_template": "At {time_str}, {container} encountered an unclassified runtime event that did not match known operational signatures.",
    "user_impact": "Unrecognized runtime behavior; event telemetry captured for engineering triage.",
    "recommendation": "Review event in Dozzle drill-down and define an operational procedure if recurring.",
    "severity": "HIGH",
    "action_required": False,
}

from windmill.flows.decision_state import sanitize_message  # noqa: F401
# Sanitisation lives in decision_state (L-003): one implementation, called before
# any egress, and re-exported here so existing callers keep working.

def synthesize_anecdote(
    event_payload: Dict[str, Any],
    verbosity: str = "user_friendly",
    *,
    fault_class: Optional[str] = None,
    decision: Optional[Any] = None,
) -> Dict[str, Any]:
    tenant_id = event_payload.get("tenant_id", "global")
    raw_msg = event_payload.get("event_meta", {}).get("raw_message", "")
    container = event_payload.get("host_meta", {}).get("container", "workload")
    timestamp = event_payload.get("event_meta", {}).get("timestamp") or datetime.datetime.utcnow().isoformat()
    status_code = event_payload.get("event_meta", {}).get("status_code", 500)
    
    try:
        dt = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        time_str = dt.strftime("%H:%M:%S UTC")
    except Exception:
        time_str = "just now"

    # Semantic template selection (L-012)
    target_fault = fault_class or (getattr(decision, "fault_class", None) if decision is not None else None)
    matched_pattern = None

    if target_fault == "unclassified":
        matched_pattern = UNCLASSIFIED_PATTERN
    elif target_fault in FAULT_CLASS_TEMPLATES:
        pat_id = FAULT_CLASS_TEMPLATES[target_fault]
        for pattern in ERROR_PATTERNS:
            if pattern["id"] == pat_id:
                matched_pattern = pattern
                break

    # Offline / Gate unavailable fallback: first-match-wins regex loop
    if not matched_pattern:
        for pattern in ERROR_PATTERNS:
            if re.search(pattern["regex"], raw_msg):
                matched_pattern = pattern
                break

    if not matched_pattern:
        matched_pattern = {
            "id": "GENERIC_APPLICATION_FAULT",
            "headline": f"Application Anomaly in {container}",
            "story_template": f"At {time_str}, {container} returned status code {status_code} while processing a workload transaction.",
            "user_impact": "Single transaction failure.",
            "recommendation": "Engineers are alerted; telemetry captured in Dozzle.",
            "severity": "MEDIUM" if status_code < 500 else "HIGH",
            "action_required": False
        }

    narrative = matched_pattern["story_template"].format(
        time_str=time_str,
        container=container,
        status_code=status_code
    )

    bulletins = [
        f"📍 Location: `{container}` on `{event_payload.get('host_meta', {}).get('hostname', 'host')}`",
        f"⏱ Time: {time_str}",
        f"📊 Severity: {matched_pattern['severity']}",
        f"👤 Customer Impact: {matched_pattern['user_impact']}",
        f"🛠 Status: {matched_pattern['recommendation']}"
    ]

    bridge = event_payload.get("bridge_protocol")
    if not bridge and "bridge=" in raw_msg:
        m = re.search(r"bridge=(\w+)", raw_msg)
        if m:
            bridge = m.group(1)
    if bridge and bridge != "none":
        bulletins.append(f"🌉 Bridge Protocol: `{bridge}`")

    logs_url = f"https://logs.domain.tld/container/{container}"

    # L-013: Derive action_required from typed gate judgments rather than regex booleans
    action_required = matched_pattern["action_required"]
    if decision is not None and getattr(decision, "evaluated", False):
        if (
            getattr(decision, "action", "none") != "none"
            or getattr(decision, "reason_code", None) == "thresholds_satisfied"
            or getattr(decision, "would_be_action", "none") not in ("none", None, "")
        ):
            action_required = True
        elif getattr(decision, "reason_code", None) in (
            "threshold_failed",
            "severity_not_permitted",
            "threshold_failed_and_severity_not_permitted",
        ):
            action_required = False
        else:
            raw = getattr(decision, "raw_answers", {}) or {}
            help_noul = (raw.get("restart_would_help") or {}).get("noul")
            discards_noul = (raw.get("restart_discards_state") or {}).get("noul")
            if help_noul is not None and discards_noul is not None:
                action_required = help_noul >= 0.7 and discards_noul <= 0.3

    anecdote = {
        "anecdote_id": matched_pattern["id"],
        "headline": f"[{matched_pattern['severity']}] {matched_pattern['headline']} (Tenant: {tenant_id})",
        "narrative": narrative,
        "sanitized_preview": sanitize_message(raw_msg)[:250],
        "customer_impact": matched_pattern["user_impact"],
        "resolution_guidance": matched_pattern["recommendation"],
        "bulletins": bulletins,
        "dozzle_drilldown_url": logs_url,
        "action_required": action_required,
    }

    if verbosity == "executive":
        anecdote["narrative"] = f"A brief {matched_pattern['headline'].lower()} occurred on tenant {tenant_id}. {matched_pattern['user_impact']} {matched_pattern['recommendation']}"

    return anecdote
