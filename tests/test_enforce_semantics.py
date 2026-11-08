#!/usr/bin/env python3
"""L-009: enforce mode, fail-closed. One section per PLAN.md §2.7 row.

The gate can withhold a restart. It cannot grant one the reported severity
and the tenant policy do not already allow. Hourly budget is checked by the
authorization function and again by the router — neither assumes the other.

    python3 tests/test_enforce_semantics.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from windmill.flows import tenant_router as tr  # noqa: E402
import test_decision_gate as tdg  # noqa: E402

FAILURES: list[str] = []
RAW = "fatal error: runtime: out of memory"
FIXTURES = ROOT / "tests" / "fixtures" / "events"
BASELINE = ROOT / "tests" / "out" / "baseline.json"

try:
    from windmill.flows.decision_gate import authorize_remediation
except ImportError:
    authorize_remediation = None


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{'' if condition else ' - ' + detail}")
    if not condition:
        FAILURES.append(label)


class TypeSafeAPITimeoutError(Exception):
    pass


class TypeSafeAPIConnectionError(Exception):
    pass


class TypeSafeRateLimitError(Exception):
    pass


class TypeSafeBadRequestError(Exception):
    def __init__(self, request_id: str):
        super().__init__("max_tokens_exceeded")
        self.request_id = request_id


class SequenceClient:
    """Raises or returns, one step per call. Records every call."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls: list = []

    def system_one(self, state=None, questions=None, model=None):
        self.calls.append({"state": state, "model": model})
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _response(answers):
    return type("R", (), {
        "answers": answers,
        "model": "jev-1.13.0",
        "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})(),
    })()


def event(*, severity="critical", raw=RAW, tenant="fintech") -> dict:
    return {
        "tenant_id": tenant, "org_id": "acme-payments", "severity": severity,
        "is_error": True,
        "tenant": {"id": tenant, "environment": "production"},
        "event_meta": {"is_error": True, "severity": severity, "status_code": 500,
                       "stream": "stderr", "raw_message": raw,
                       "timestamp": "2026-09-01T23:46:00Z"},
        "host_meta": {"container": "payment_gateway", "hostname": "debian-node-01"},
    }


def write_policies(tmp: Path, *, enabled=True, mode="enforce", shadow=False,
                   remediation=True, max_restarts=3, allowed=("critical",),
                   target_action="restart_container", model="jev-1.13.0",
                   rate_limit_backoff_s=1.0) -> Path:
    d = tmp / "tenants"
    d.mkdir(parents=True, exist_ok=True)
    gate = dict(tdg.policy()["decision_gate"])
    gate.update({
        "enabled": enabled, "mode": mode, "shadow": shadow, "model": model,
        "allowed_severities": list(allowed),
        "rate_limit_backoff_s": rate_limit_backoff_s,
    })
    auto = {"enabled": remediation, "target_action": target_action}
    if max_restarts is not None:
        auto["max_restarts_per_hour"] = max_restarts
    (d / "platform.yaml").write_text(yaml.safe_dump({
        "tenant_id": "platform", "silenced": False,
        "routing": {"default_sink": "matrix", "matrix_room_id": "!platform:test"},
        "decision_gate": {"enabled": False, "mode": "shadow"},
    }))
    (d / "fintech.yaml").write_text(yaml.safe_dump({
        "tenant_id": "fintech", "silenced": False,
        "routing": {"default_sink": "matrix", "matrix_room_id": "!fintech:test"},
        "severity_overrides": {"critical": ["matrix"], "error": ["matrix"]},
        "auto_remediation": auto,
        "decision_gate": gate,
    }))
    return d


def use(directory: Path) -> None:
    tr.POLICY_DIR = str(directory)
    tr._policy_index = None
    tr.build_policy_index(force=True)


def markers(response: dict) -> list:
    return [d for d in response.get("dispatched_to", []) if str(d).startswith("triggered_remediation_")]


def main() -> int:
    anecdotes: list = []
    dlq: list = []
    alerts: list = []
    restarts: list = []
    slept: list = []

    def capture_matrix(room, anecdote, policy):
        anecdotes.append({"room": room, "anecdote": anecdote})

    def capture_dlq(record):
        dlq.append(record)
        return record

    def capture_alert(record):
        alerts.append(record)
        return record

    def counting_restart(container, action):
        restarts.append((container, action))
        return "triggered_remediation_" + str(action) + "_on_" + str(container)

    def record_sleep(seconds):
        slept.append(seconds)

    tr.dispatch_matrix_anecdote = capture_matrix
    tr.dispatch_slack_anecdote = lambda *a, **k: None
    real_enqueue = tr.enqueue_dlq
    tr.enqueue_dlq = capture_dlq
    tr.alert_platform = capture_alert
    tr.perform_restart = counting_restart

    def route(ev, client, tmp, **kw):
        anecdotes.clear()
        dlq.clear()
        alerts.clear()
        restarts.clear()
        slept.clear()
        # restarts_this_hour is a call argument, not a policy field.
        taken = kw.pop("restarts_this_hour", 0)
        use(write_policies(tmp, **kw))
        return tr.main(ev, gate_client=client, gate_schema=tdg.SCHEMA,
                       restarts_this_hour=taken, sleep=record_sleep)

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)

        print("\nfailure table: timeout / connection — no action, anecdote flagged")
        for label, exc in (("timeout", TypeSafeAPITimeoutError("timed out")),
                           ("connection", TypeSafeAPIConnectionError("reset"))):
            client = tdg.StubClient(raises=exc)
            r = route(event(), client, tmp / label)
            check(label + ": action none", r.get("enforce", {}).get("action") == "none", str(r.get("enforce")))
            check(label + ": no restart", not markers(r) and not restarts, str(r.get("dispatched_to")))
            check(label + ": decision_unavailable on the response",
                  r.get("decision_unavailable") is True, str(r.get("decision_unavailable")))
            check(label + ": anecdote was routed with the flag",
                  anecdotes and anecdotes[-1]["anecdote"].get("decision_unavailable") is True
                  and any("decision_unavailable: true" in b for b in anecdotes[-1]["anecdote"].get("bulletins", [])),
                  str(anecdotes[-1]["anecdote"] if anecdotes else None))
            check(label + ": not retried", len(client.calls) == 1 and not slept,
                  f"calls={len(client.calls)} slept={slept}")
            check(label + ": raw message did not ride the flag",
                  RAW not in json.dumps(anecdotes[-1]["anecdote"].get("bulletins")),
                  "bulletin leaked the raw line")

        print("\nfailure table: bad request — no action, request id alerted, never retried")
        bad = TypeSafeBadRequestError("req-l009")
        client = tdg.StubClient(raises=bad)
        r = route(event(), client, tmp / "bad")
        check("bad request: action none", r.get("enforce", {}).get("action") == "none", str(r.get("enforce")))
        check("bad request: no restart", not markers(r), str(markers(r)))
        check("bad request: exactly one call", len(client.calls) == 1, str(len(client.calls)))
        check("bad request: platform alert carried the request id",
              alerts and alerts[-1].get("request_id") == "req-l009", str(alerts))
        check("bad request: alert has no log content",
              alerts and RAW not in json.dumps(alerts[-1]), str(alerts))
        check("bad request: side effect is the platform alert",
              r.get("enforce", {}).get("side_effect") == "alert_platform", str(r.get("enforce")))

        print("\\nfailure table: 429 — retry once, then DLQ, no action")
        client = SequenceClient([TypeSafeRateLimitError("429"), TypeSafeRateLimitError("429 again")])
        r = route(event(), client, tmp / "rate", rate_limit_backoff_s=0.5)
        check("429: action none", r.get("enforce", {}).get("action") == "none", str(r.get("enforce")))
        check("429: no restart", not markers(r), str(markers(r)))
        check("429: retried exactly once", len(client.calls) == 2, str(len(client.calls)))
        check("429: backoff was requested once", slept == [0.5], str(slept))
        check("429: DLQ received the failure",
              dlq and dlq[-1].get("error_class") == "gate_rate_limited"
              and dlq[-1].get("attempt_count") == 2, str(dlq))
        check("429: DLQ record has no log content",
              dlq and RAW not in json.dumps(dlq[-1]) and "raw_message" not in json.dumps(dlq[-1]),
              str(dlq))
        check("429: side effect is dlq", r.get("enforce", {}).get("side_effect") == "dlq",
              str(r.get("enforce")))

        print("\n429 then success is a retry, not a dead end")
        ok = _response(tdg.answers())
        client = SequenceClient([TypeSafeRateLimitError("429"), ok])
        r = route(event(), client, tmp / "rate-ok")
        check("429-then-ok: exactly one restart", len(restarts) == 1 and len(markers(r)) == 1,
              f"restarts={restarts} markers={markers(r)}")
        check("429-then-ok: no DLQ", not dlq, str(dlq))
        check("429-then-ok: one backoff", len(slept) == 1, str(slept))

        print("\nfailure table: low confidence — no action, REVIEW REQUIRED")
        client = tdg.StubClient(tdg.answers(sev_conf=0.42))
        r = route(event(), client, tmp / "low")
        check("low confidence: action none", r.get("enforce", {}).get("action") == "none", str(r.get("enforce")))
        check("low confidence: no restart", not markers(r), str(markers(r)))
        check("low confidence: tenant sink carries REVIEW REQUIRED",
              anecdotes and anecdotes[-1]["anecdote"].get("headline", "").startswith("REVIEW REQUIRED"),
              str(anecdotes[-1]["anecdote"].get("headline") if anecdotes else None))
        check("low confidence: side effect is escalate",
              r.get("enforce", {}).get("side_effect") == "escalate", str(r.get("enforce")))

        print("\nfailure table: model contradicts policy — policy wins, no action")
        client = tdg.StubClient(tdg.answers())
        r = route(event(), client, tmp / "policy", allowed=("error",))
        check("policy contradiction: action none", r.get("enforce", {}).get("action") == "none",
              str(r.get("enforce")))
        check("policy contradiction: no restart", not markers(r), str(markers(r)))
        check("policy contradiction: the model was asked and still withheld",
              len(client.calls) == 1, str(len(client.calls)))
        client = tdg.StubClient(tdg.answers())
        r = route(event(), client, tmp / "policy-off", remediation=False)
        check("remediation disabled: no restart even if the stub would approve",
              not markers(r), str(markers(r)))
        check("remediation disabled: the model is not consulted to grant it",
              not client.calls, str(len(client.calls)))

        print("\nhigh confidence, policy permitted, budget open: exactly one restart")
        client = tdg.StubClient(tdg.answers())
        r = route(event(), client, tmp / "happy", restarts_this_hour=0)
        check("happy: exactly one restart call", len(restarts) == 1, str(restarts))
        check("happy: exactly one marker", markers(r) == [
            "triggered_remediation_restart_container_on_payment_gateway"], str(markers(r)))
        check("happy: side effect is restart", r.get("enforce", {}).get("side_effect") == "restart",
              str(r.get("enforce")))
        check("happy: the gate was asked once", len(client.calls) == 1, str(len(client.calls)))

        print("\nbudget exhausted: the gate withholds, and the router does not trust the gate")
        check("authorize_remediation exists", authorize_remediation is not None)
        if authorize_remediation is not None:
            decision = tdg.run(client=tdg.StubClient())
            auth = authorize_remediation(decision, tdg.policy(), reported_severity="critical",
                                         restarts_this_hour=3)
            check("gate check: count == cap does not fire", auth.get("fire") is False
                  and auth.get("reason") == "budget_exhausted", str(auth))
            auth0 = authorize_remediation(decision, tdg.policy(), reported_severity="critical",
                                          restarts_this_hour=0)
            check("gate check: count below cap may fire", auth0.get("fire") is True, str(auth0))
            missing = dict(tdg.policy())
            missing["auto_remediation"] = dict(missing["auto_remediation"])
            del missing["auto_remediation"]["max_restarts_per_hour"]
            auth_missing = authorize_remediation(decision, missing, reported_severity="critical",
                                                 restarts_this_hour=0)
            check("gate check: a missing cap is not assumed unlimited",
                  auth_missing.get("fire") is False, str(auth_missing))

        client = tdg.StubClient(tdg.answers())
        r = route(event(), client, tmp / "budget", restarts_this_hour=3)
        check("router: budget exhausted withholds the restart", not markers(r), str(markers(r)))
        client = tdg.StubClient(tdg.answers())
        r = route(event(), client, tmp / "no-cap", max_restarts=None)
        check("router: a missing cap is not assumed unlimited", not markers(r), str(markers(r)))

        print("\nhourly file counter: production entry point, no injected count")
        saved_budget = os.environ.pop("GATE_BUDGET_DIR", None)
        budget_dir = tmp / "gate-budget"
        budget_dir.mkdir()
        os.environ["GATE_BUDGET_DIR"] = str(budget_dir)
        frozen = 1_700_000_000.0
        real_time = tr.time.time
        tr.time.time = lambda: frozen
        try:
            use(write_policies(tmp / "hour-file", max_restarts=1))
            client = tdg.StubClient(tdg.answers())
            anecdotes.clear()
            dlq.clear()
            alerts.clear()
            restarts.clear()
            slept.clear()
            first = tr.main(event(), gate_client=client, gate_schema=tdg.SCHEMA, sleep=record_sleep)
            check("file counter: first call fires once",
                  len(restarts) == 1 and len(markers(first)) == 1
                  and first.get("enforce", {}).get("side_effect") == "restart",
                  f"restarts={restarts} markers={markers(first)} enforce={first.get('enforce')}")
            client = tdg.StubClient(tdg.answers())
            anecdotes.clear()
            dlq.clear()
            alerts.clear()
            restarts.clear()
            slept.clear()
            second = tr.main(event(), gate_client=client, gate_schema=tdg.SCHEMA, sleep=record_sleep)
            check("file counter: second call in the same hour does not fire",
                  not restarts and not markers(second)
                  and second.get("enforce", {}).get("action") == "none",
                  f"restarts={restarts} markers={markers(second)} enforce={second.get('enforce')}")
        finally:
            tr.time.time = real_time
            if saved_budget is None:
                os.environ.pop("GATE_BUDGET_DIR", None)
            else:
                os.environ["GATE_BUDGET_DIR"] = saved_budget

        def lying(*_a, **_k):
            return {"fire": True, "action": "restart_container", "reason": "authorized",
                    "policy_permitted": True, "budget_ok": True, "gate_ok": True, "reported_ok": True}

        original = getattr(tr, "authorize_remediation", None)
        tr.authorize_remediation = lying
        try:
            client = tdg.StubClient(tdg.answers())
            r = route(event(), client, tmp / "lie-budget", restarts_this_hour=3)
            check("router re-checks the budget even if authorize says fire",
                  not markers(r), str(markers(r)))
            client = tdg.StubClient(tdg.answers())
            r = route(event(), client, tmp / "lie-policy", remediation=False, restarts_this_hour=0)
            check("router re-checks policy even if authorize says fire",
                  not markers(r), str(markers(r)))
        finally:
            if original is not None:
                tr.authorize_remediation = original

        print("\nthe gate cannot grant what reported severity would not")
        client = tdg.StubClient(tdg.answers(severity="critical"))
        r = route(event(severity="error"), client, tmp / "no-grant")
        check("reported error is not restarted just because the gate says critical",
              not markers(r), str(markers(r)))

        print("\naction_required is not the enforce predicate")
        client = tdg.StubClient(tdg.answers())
        r = route(event(raw=RAW), client, tmp / "oom")
        check("OOM (action_required false) still restarts when gate, policy and budget agree",
              len(markers(r)) == 1, str(markers(r)))
        check("the router source does not read action_required",
              "action_required" not in Path(tr.__file__).read_text(encoding="utf-8"))

        print("\nenabled false returns to today's path")
        client = tdg.StubClient(tdg.answers(sev_conf=0.1))
        r = route(event(), client, tmp / "off", enabled=False, mode="enforce")
        check("enabled false: a withholding gate is not consulted", not client.calls,
              str(len(client.calls)))
        check("enabled false: reported critical still restarts, as today",
              markers(r) == ["triggered_remediation_restart_container_on_payment_gateway"],
              str(markers(r)))
        check("enabled false: no enforce block on the response", "enforce" not in r, str(r.keys()))

        print("\nL-001 baseline: shipped policies (enabled false) still dispatch as recorded")
        baseline = {row["fixture"]: row for row in json.loads(BASELINE.read_text())["rows"]}
        use(ROOT / "rules" / "tenants")
        drift = []
        for path in sorted(FIXTURES.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            fid = payload.get("fixture_id", path.stem)
            if fid not in baseline:
                continue
            got = tr.main(payload).get("dispatched_to")
            if got != baseline[fid]["dispatched_to"]:
                drift.append((fid, got, baseline[fid]["dispatched_to"]))
        check("every fixture matches the L-001 dispatched_to", not drift, str(drift[:3]))

        print("\na gate error in enforce withholds; it does not fall through to today's restart")
        client = tdg.StubClient()
        r = route(event(), client, tmp / "alias", model="jev-latest")
        check("alias model: no restart", not markers(r), str(markers(r)))
        check("alias model: the client was not used to paper over the config error",
              not client.calls, str(len(client.calls)))

        print("\ndefault DLQ writer: one file, no log content, no directory means not queued")
        import os as _os
        saved_dlq = _os.environ.pop("GATE_DLQ_DIR", None)
        try:
            bare = real_enqueue({"error_class": "gate_rate_limited", "decision_id": "abc123",
                                 "attempt_count": 2, "raw_message": RAW})
        finally:
            if saved_dlq is not None:
                _os.environ["GATE_DLQ_DIR"] = saved_dlq
        check("unset GATE_DLQ_DIR does not pretend the item was queued",
              bare.get("queued") is False and "path" not in bare, str(bare))
        check("a raw_message key is stripped before return", "raw_message" not in bare, str(bare))
        with tempfile.TemporaryDirectory() as dlq_dir:
            written = real_enqueue(
                {"error_class": "gate_rate_limited", "decision_id": "abc123",
                 "attempt_count": 2, "tenant_id": "fintech"},
                directory=dlq_dir,
            )
            files = list(Path(dlq_dir).glob("*.json"))
            body = files[0].read_text(encoding="utf-8") if files else ""
            check("one file was written", len(files) == 1 and written.get("queued") is True,
                  str(files))
            check("the file names the failure and not the log line",
                  "gate_rate_limited" in body and RAW not in body and "raw_message" not in body,
                  body)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for label in FAILURES:
            print(f"  - {label}")
        return 1
    print("all enforce-semantics checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
