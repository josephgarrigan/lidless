#!/usr/bin/env python3
"""Generate the L-001 fixture corpus deterministically.

Writes:
    tests/fixtures/events/*.json   Vector event envelopes
    tests/fixtures/labels.yaml     human labels + rationale

Deterministic by construction: no RNG, no clock. Re-running produces byte-identical
output. Every fixture is synthetic; any credential, IP, or token in it is fake and
exists so the sanitizer and redaction tests (L-003) have material to work on.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
EVENTS = HERE / "events"

CONTAINERS = ["payment_gateway", "ledger_worker", "auth_edge", "orders_api", "search_index", "batch_exporter"]
HOSTS = ["debian-node-01", "debian-node-02", "debian-node-03"]
TENANTS = [("fintech", "acme-payments", "production"), ("alpha-corp", "alpha-enterprises", "production")]

# Keyword phrasings drawn from each ERROR_PATTERNS regex in anecdote_engine.py.
CLASS_MESSAGES = {
    "DB_CONNECTION_EXHAUSTION": [
        "pq: remaining connection slots are reserved for non-replication superuser connections at 10.10.2.6:5432 with password=secret_pw123",
        "could not connect to server: Connection refused (is the server running on host 10.10.2.6)",
        "too many connections for role ledger_app",
        "connection pool exhausted after 30s waiting for a free slot",
        "connection timeout after 5000ms to upstream database",
        "deadlock detected while updating ledger_transactions",
        "connection reset by peer during transaction commit",
        "remaining connection slots are reserved for superuser connections",
    ],
    "OOM_MEMORY_CEILING": [
        "fatal error: runtime: out of memory - killed process payment_gateway",
        "oom killed container ledger_worker (memory cgroup out of memory)",
        "cannot allocate memory: mmap failed for 2097152 bytes",
        "killed process 4181 (orders_api) total-vm:3012345kB",
        "out of memory while building search index shard 7",
        "memory cgroup out of memory: Killed process 9912 (batch_exporter)",
        "runtime: out of memory: cannot allocate 1048576-byte block (8388608 in use)",
        "oom: process auth_edge was terminated by the kernel",
    ],
    "AUTH_SECURITY_REJECTION": [
        "jwt expired at 2026-09-01T23:44:00Z - token rejected",
        "token signature invalid for issuer https://auth.example.org",
        "401 unauthorized: bearer credential missing",
        "permission denied for service account ledger-sync",
        "403 forbidden: scope repo.code required",
        "TLS handshake error from 10.10.1.9:52144: bad certificate",
        "access_denied: client not permitted for requested audience",
        "unauthorized: token audience mismatch (expected api://ledger)",
    ],
    "UPSTREAM_TIMEOUT": [
        "504 gateway timeout from payments_upstream",
        "502 bad gateway while proxying to ledger_upstream",
        "upstream request timeout after 10s",
        "context deadline exceeded calling risk-service",
        "request to https://partner.example/api timed out",
        "deadline exceeded: put object to s3 archive bucket",
        "504 gateway timeout on POST /api/v1/t/fintech/charge",
        "upstream request timeout (read) on keep-alive connection",
    ],
    "RUNTIME_PANIC": [
        "panic: runtime error: index out of range [7] with length 5",
        "fatal error: concurrent map writes",
        "segmentation fault (core dumped) in libcrypto.so",
        "NullPointerException in PaymentLedgerService.reconcile()",
        "uncaught exception: ValueError: invalid literal for int()",
        "core dumped while writing index shard journal",
        "panic: nil pointer dereference in order router",
        "fatal error: all goroutines are asleep - deadlock!",
    ],
}

# Human-intended class differs from first-match-wins for these: the second class's
# keyword appears in the message, but ERROR_PATTERNS order puts the first one earlier.
# ERROR_PATTERNS order is: DB, OOM, AUTH, UPSTREAM, PANIC.
MULTI_PATTERN = [
    ("OOM_MEMORY_CEILING", "panic: out of memory", "OOM is the cause; the panic is its symptom"),
    ("AUTH_SECURITY_REJECTION", "unauthorized: connection timeout talking to the token service", "auth rejection is the user-visible fault"),
    ("UPSTREAM_TIMEOUT", "deadlock detected after gateway timeout", "the timeout is what the customer saw"),
    ("RUNTIME_PANIC", "cannot allocate memory: segfault during allocation", "the segfault is the fault"),
    ("DB_CONNECTION_EXHAUSTION", "too many connections followed by panic: pool monitor crashed", "pool exhaustion is the cause"),
]

# Faults no pattern matches, but which a human would call an incident.
NOVEL = [
    ("disk_quota_exceeded", "disk quota exceeded on volume /var/lib/docker for ledger_worker", "critical", "storage exhaustion, not covered by any pattern"),
    ("tls_clock_skew", "x509: certificate has expired or is not yet valid: current time is before notBefore", "error", "clock/cert problem with no matching keyword"),
    ("nats_slow_consumer", "slow consumer detected on stream orders: pending messages 41233", "error", "queue backlog, no matching keyword"),
]

BENIGN = [
    ("startup_banner", "server listening on :8080", 200, "info"),
    ("health_probe", "GET /healthz 200 in 1.2ms", 200, "info"),
    ("gatus_probe_ok", "probe succeeded: https://orders.example/healthz", 200, "info"),
    ("migration_applied", "applied migration 0042_add_ledger_index", 200, "info"),
    ("request_ok", "POST /api/v1/t/fintech/charge 201 in 84ms", 201, "info"),
    ("cache_warm", "cache warm complete: 18432 keys", 200, "info"),
    ("graceful_shutdown", "received SIGTERM, draining connections", 200, "warning"),
    ("slow_query_ok", "query completed in 4210ms (no error)", 200, "warning"),
    ("retry_recovered", "upstream retry succeeded on attempt 3", 200, "info"),
    ("config_reload", "configuration reloaded from /etc/ledger/config.yaml", 200, "info"),
]


def envelope(fid, tenant, org, env, container, host, message, status, stream, is_error, severity, ts):
    return {
        "fixture_id": fid,
        "tenant_id": tenant,
        "org_id": org,
        "team_id": "payments",
        "severity": severity,
        "is_error": is_error,
        "tenant": {"id": tenant, "org": org, "team": "payments", "environment": env},
        "event_meta": {
            "is_error": is_error,
            "severity": severity,
            "status_code": status,
            "stream": stream,
            "raw_message": message,
            "timestamp": ts,
        },
        "host_meta": {"container": container, "hostname": host},
    }


def main() -> int:
    EVENTS.mkdir(parents=True, exist_ok=True)
    for old in EVENTS.glob("*.json"):
        old.unlink()

    labels = []

    def emit(fid, payload, fault_class, severity_band, remediation, rationale, tags, intended_class=None):
        (EVENTS / f"{fid}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        labels.append({
            "fixture": fid, "fault_class": fault_class, "severity_band": severity_band,
            "remediation_expected": remediation, "rationale": rationale, "tags": tags,
            "intended_class": intended_class,
        })

    ts_counter = 0

    def next_ts():
        nonlocal ts_counter
        ts_counter += 1
        return f"2026-09-01T23:{ts_counter:02d}:00Z"

    # --- 8 per class ------------------------------------------------------
    for cls, messages in CLASS_MESSAGES.items():
        for i, msg in enumerate(messages):
            tenant, org, env = TENANTS[i % len(TENANTS)]
            status = 500 if cls != "AUTH_SECURITY_REJECTION" else 401
            sev = "critical" if cls in ("OOM_MEMORY_CEILING", "RUNTIME_PANIC") else "error"
            band = sev
            remediation = cls in ("OOM_MEMORY_CEILING", "DB_CONNECTION_EXHAUSTION")
            emit(
                f"{cls.lower()}_{i + 1:02d}",
                envelope(f"{cls.lower()}_{i + 1:02d}", tenant, org, env, CONTAINERS[i % len(CONTAINERS)],
                         HOSTS[i % len(HOSTS)], msg, status, "stderr", True, sev, next_ts()),
                cls, band, remediation,
                f"single-pattern {cls} fixture {i + 1}",
                ["single_pattern"],
            )

    # --- multi-pattern (AMBIGUOUS: excluded from accuracy figures) ---------
    # Ruling of 2026-09-20: which class "really" applies is a judgement call, so these
    # carry the intended class for the ordering test but no agreed answer to score against.
    for i, (cls, msg, rationale) in enumerate(MULTI_PATTERN):
        tenant, org, env = TENANTS[i % len(TENANTS)]
        emit(
            f"multi_pattern_{i + 1:02d}",
            envelope(f"multi_pattern_{i + 1:02d}", tenant, org, env, CONTAINERS[i % len(CONTAINERS)],
                     HOSTS[i % len(HOSTS)], msg, 500, "stderr", True, "critical", next_ts()),
            "AMBIGUOUS", "critical", True,
            f"{rationale}; the label is a judgement call, so this fixture is excluded from accuracy figures",
            ["multi_pattern", "ambiguous"], intended_class=cls,
        )

    # --- novel ------------------------------------------------------------
    for i, (slug, msg, band, rationale) in enumerate(NOVEL):
        tenant, org, env = TENANTS[i % len(TENANTS)]
        emit(
            f"novel_{slug}",
            envelope(f"novel_{slug}", tenant, org, env, CONTAINERS[i % len(CONTAINERS)],
                     HOSTS[i % len(HOSTS)], msg, 500, "stderr", True, "error", next_ts()),
            "UNCLASSIFIED", band, False, rationale, ["novel"],
        )

    # --- benign -----------------------------------------------------------
    for i, (slug, msg, status, band) in enumerate(BENIGN):
        tenant, org, env = TENANTS[i % len(TENANTS)]
        emit(
            f"benign_{slug}",
            envelope(f"benign_{slug}", tenant, org, env, CONTAINERS[i % len(CONTAINERS)],
                     HOSTS[i % len(HOSTS)], msg, status, "stdout", False, "info", next_ts()),
            "NONE", "none", False, f"non-error event ({slug}); must not be routed", ["benign"],
        )

    counts: dict[str, int] = {}
    for entry in labels:
        for tag in entry["tags"]:
            counts[tag] = counts.get(tag, 0) + 1

    header = (
        "# L-001 fixture labels. Human-assigned; the ground truth for accuracy comparisons.\n"
        "# fault_class: one of the five ERROR_PATTERNS ids, or UNCLASSIFIED / NONE.\n"
        "# severity_band: critical | error | warning | none (human judgement, not the regex's).\n"
        "# remediation_expected: human judgement on whether a container restart is appropriate.\n"
        f"# fixtures: {len(labels)} ({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))})\n"
    )
    labels_path = HERE / "labels.yaml"
    lines = [header, "labels:\n"]
    for entry in labels:
        lines.append(f"  - fixture: {entry['fixture']}\n")
        lines.append(f"    fault_class: {entry['fault_class']}\n")
        lines.append(f"    severity_band: {entry['severity_band']}\n")
        lines.append(f"    remediation_expected: {'true' if entry['remediation_expected'] else 'false'}\n")
        lines.append(f"    tags: [{', '.join(entry['tags'])}]\n")
        if entry.get("intended_class"):
            lines.append(f"    intended_class: {entry['intended_class']}\n")
        lines.append(f"    rationale: \"{entry['rationale']}\"\n")
    labels_path.write_text("".join(lines), encoding="utf-8")

    print(f"wrote {len(labels)} fixtures to {EVENTS}")
    print(f"wrote labels to {labels_path}")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
