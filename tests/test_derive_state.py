#!/usr/bin/env python3
"""L-003 tests: the derived-state contract (ADR-0001).

Asserts the payload boundary holds: sanitisation runs, no raw log text escapes in
`structured_only`, the mode is never defaulted, and the input is never mutated.

Usage:
    python3 tests/test_derive_state.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from windmill.flows import anecdote_engine as ae  # noqa: E402
from windmill.flows import decision_state as ds  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{'' if condition else ' - ' + detail}")
    if not condition:
        FAILURES.append(label)


def envelope(raw_message: str, **event_over) -> dict:
    event = {"is_error": True, "severity": "critical", "status_code": 500, "stream": "stderr",
             "raw_message": raw_message, "timestamp": "2026-09-01T23:46:00Z"}
    event.update(event_over)
    return {
        "tenant_id": "fintech", "org_id": "acme-payments", "severity": "critical", "is_error": True,
        "tenant": {"id": "fintech", "org": "acme-payments", "team": "payments", "environment": "production"},
        "event_meta": event,
        "host_meta": {"container": "payment_gateway", "hostname": "debian-node-01"},
    }


def main() -> int:
    print("redaction:")
    cases = [
        ("pq: remaining slots at 10.10.2.6:5432 with password=secret_pw123",
         ["10.10.2.6", "secret_pw123"]),
        ("auth failed: Bearer eyJhbGciOiJIUzI1NiJ9.abc", ["eyJhbGciOiJIUzI1NiJ9.abc"]),
        ("token: abc123def456 rejected", ["abc123def456"]),
        ("wrote /var/lib/docker/volumes/ledger/_data/journal.db", ["volumes"]),
        ("key=supersecretvalue rotated", ["supersecretvalue"]),
    ]
    for raw, must_vanish in cases:
        cleaned = ds.sanitize_message(raw)
        for token in must_vanish:
            check(f"redacts {token!r}", token not in cleaned, f"still present in {cleaned!r}")
        check(f"keeps the message readable: {raw[:28]!r}", bool(cleaned.strip()))

    print("\nsanitize_message is one implementation:")
    check("anecdote_engine re-exports the same function",
          ae.sanitize_message is ds.sanitize_message)

    print("\nmode is explicit:")
    for bad in ("", None, "sanitized", "STRUCTURED_ONLY", "everything"):
        raised = False
        try:
            ds.derive_state(envelope("panic: boom"), bad)  # type: ignore[arg-type]
        except ds.StateModeError:
            raised = True
        check(f"mode {bad!r} raises StateModeError", raised)
    raised = False
    try:
        ds.derive_state(envelope("panic: boom"))  # type: ignore[call-arg]
    except TypeError:
        raised = True
    check("omitting the mode is a TypeError, not a silent default", raised)

    print("\nstructured_only carries no log text:")
    raw = ("pq: remaining connection slots are reserved for non-replication superuser "
           "connections at 10.10.2.6:5432 with password=secret_pw123")
    env = envelope(raw)
    state = ds.derive_state(env, "structured_only")
    dumped = json.dumps(state)
    # Tokens that survive legitimately are the ones the envelope also carries as
    # structured fields (container name, hostname, tenant). Anything else is a leak.
    structured = json.dumps({k: env[k] for k in ("tenant_id", "org_id", "tenant", "host_meta")})
    tokens = [t for t in re.findall(r"[A-Za-z0-9_./:\-]{6,}", raw) if t not in structured]
    leaked = [t for t in tokens if t in dumped]
    check("no raw_message token escapes", not leaked, f"leaked {leaked}")
    check("raw_message key is absent", "raw_message" not in state)
    check("sanitized_preview is absent in this mode", "sanitized_preview" not in state)
    check("structured fields survive",
          state["container"] == "payment_gateway" and state["status_code"] == 500)

    print("\nsanitized_content carries only the redacted preview:")
    state = ds.derive_state(env, "sanitized_content")
    check("preview equals sanitize_message()", state["sanitized_preview"] == ds.sanitize_message(raw))
    check("preview is redacted", "10.10.2.6" not in state["sanitized_preview"]
          and "secret_pw123" not in state["sanitized_preview"])
    check("preview is capped", len(state["sanitized_preview"]) <= ds.MAX_PREVIEW_CHARS)

    print("\npure function:")
    env = envelope("panic: boom")
    before = json.dumps(env, sort_keys=True)
    ds.derive_state(env, "sanitized_content")
    check("input is not mutated", json.dumps(env, sort_keys=True) == before)

    print("\nfingerprint:")
    a = ds.derive_state(envelope("panic: boom"), "structured_only")
    b = ds.derive_state(envelope("panic: boom"), "structured_only")
    check("identical state fingerprints identically", ds.state_fingerprint(a) == ds.state_fingerprint(b))

    # Documented consequence of structured_only: the log message is deliberately not in
    # the state, so two DIFFERENT faults in the same container at the same status code
    # fingerprint the same. L-011's decision cache must not let that collapse two distinct
    # faults into one decision - its guardrail says so, and this is the measurement behind it.
    c = ds.derive_state(envelope("panic: a completely different failure"), "structured_only")
    check("structured_only cannot separate faults by message (cache must not merge them)",
          ds.state_fingerprint(a) == ds.state_fingerprint(c))

    d = ds.derive_state(envelope("panic: boom"), "sanitized_content")
    e = ds.derive_state(envelope("panic: a completely different failure"), "sanitized_content")
    check("sanitized_content does separate them", ds.state_fingerprint(d) != ds.state_fingerprint(e))

    f = ds.derive_state(envelope("panic: boom", status_code=502), "structured_only")
    check("a changed structured field changes the fingerprint",
          ds.state_fingerprint(a) != ds.state_fingerprint(f))

    check("mode is part of the fingerprint",
          ds.state_fingerprint(a) != ds.state_fingerprint(ds.derive_state(envelope("panic: boom"), "sanitized_content")))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("all derive_state checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
