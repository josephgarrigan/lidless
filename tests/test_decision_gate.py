#!/usr/bin/env python3
"""L-005 tests: the decision gate returns a decision and nothing else.

Runs under plain python3. The SDK is not needed: answers are duck-typed, the
client is stubbed, and the question schema is injected. The `--live` section
needs the venv interpreter (see tests/README.md).

Usage:
    python3 tests/test_decision_gate.py
    ~/projects/typesafe/.venv/bin/python tests/test_decision_gate.py --live
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from windmill.flows import decision_gate as dg  # noqa: E402

FAILURES: list[str] = []
QUESTIONS = {"fault_class": None, "severity": None, "is_transient": None,
             "restart_would_help": None, "restart_discards_state": None}
SCHEMA = {"version": "v1", "questions": QUESTIONS, "model": "jev-1.13.0"}


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{'' if condition else ' - ' + detail}")
    if not condition:
        FAILURES.append(label)


def envelope(raw_message: str = "fatal error: runtime: out of memory", **over) -> dict:
    payload = {
        "tenant_id": "fintech", "org_id": "acme-payments", "severity": over.pop("severity", "critical"),
        "is_error": True,
        "tenant": {"id": "fintech", "environment": "production"},
        "event_meta": {"is_error": True, "severity": "critical", "status_code": 500, "stream": "stderr",
                       "raw_message": raw_message, "timestamp": "2026-09-01T23:46:00Z"},
        "host_meta": {"container": "payment_gateway", "hostname": "debian-node-01"},
    }
    payload.update(over)
    return payload


def policy(**gate_over) -> dict:
    gate = {
        "enabled": True, "mode": "shadow", "model": "jev-1.13.0",
        "state_mode": "sanitized_content", "questions_version": "v1",
        "thresholds": {"restart_container": {"min_severity_confidence": 0.90,
                                             "min_restart_would_help": 0.85,
                                             "max_restart_discards_state": 0.15}},
        "on_low_confidence": "escalate",
    }
    gate.update(gate_over)
    return {
        "tenant_id": "fintech",
        "auto_remediation": {"enabled": True, "target_action": "restart_container",
                             "max_restarts_per_hour": 3},
        "decision_gate": gate,
    }


def answers(choice="oom_memory_ceiling", choice_conf=0.98, severity="critical", sev_conf=0.95,
            help_=0.91, discards=0.08, transient=0.2) -> dict:
    return {
        "fault_class": SimpleNamespace(choice=choice, confidence=choice_conf,
                                       probabilities={choice: choice_conf}),
        "severity": SimpleNamespace(choice=severity, confidence=sev_conf,
                                    probabilities={severity: sev_conf}),
        "is_transient": SimpleNamespace(noul=transient),
        "restart_would_help": SimpleNamespace(noul=help_),
        "restart_discards_state": SimpleNamespace(noul=discards),
    }


class StubClient:
    def __init__(self, ans=None, raises=None):
        self.ans = ans or answers()
        self.raises = raises
        self.calls: list = []

    def system_one(self, state=None, questions=None, model=None):
        self.calls.append({"state": state, "questions": questions, "model": model})
        if self.raises:
            raise self.raises
        return SimpleNamespace(answers=self.ans, model=model,
                               usage=SimpleNamespace(input_tokens=985, output_tokens=180))


def err(name: str) -> Exception:
    return type(name, (Exception,), {})(f"{name} raised")


def run(event=None, pol=None, **kw):
    return dg.evaluate(event or envelope(), pol or policy(), schema=SCHEMA, **kw)


def names(d) -> list:
    return [c.get("check") for c in d.failed_checks]


def main() -> int:
    print("the happy path proposes, it does not act:")
    client = StubClient()
    d = run(client=client)
    check("action is the policy's target_action", d.action == "restart_container", d.action)
    check("decision is marked evaluated", d.evaluated is True)
    check("not escalated", d.escalated is False)
    check("reason_code records the basis", d.reason_code == "thresholds_satisfied", d.reason_code)
    check("no failed checks", d.failed_checks == [], str(d.failed_checks))
    check("severity and fault class are carried",
          d.severity == "critical" and d.fault_class == "oom_memory_ceiling")
    check("model version comes from the response", d.model_version == "jev-1.13.0")
    check("usage is captured", d.usage.get("input_tokens") == 985, str(d.usage))
    check("exactly one call was made", len(client.calls) == 1, str(len(client.calls)))
    check("the state sent is the derived state, not the envelope",
          "raw_message" not in client.calls[0]["state"]
          and client.calls[0]["state"]["state_mode"] == "sanitized_content")

    print("\nNoul answers are not given a confidence they do not have:")
    check("restart_would_help has noul only",
          set(d.raw_answers["restart_would_help"]) == {"noul"},
          str(d.raw_answers["restart_would_help"]))
    check("no invented confidence on any noul",
          all("confidence" not in d.raw_answers[k]
              for k in ("is_transient", "restart_would_help", "restart_discards_state")))
    check("choice answers keep their distribution", "probabilities" in d.raw_answers["fault_class"])

    print("\nthresholds can only withhold:")
    d = run(client=StubClient(answers(help_=0.40)))
    check("low restart_would_help withholds the action", d.action == "none", d.action)
    check("it escalates", d.escalated is True)
    check("reason_code is threshold_failed", d.reason_code == "threshold_failed", d.reason_code)
    check("the failing check is named structurally", names(d) == ["min_restart_would_help"], str(names(d)))
    check("the summary states actual against threshold",
          "0.4" in d.reason_summary and "0.85" in d.reason_summary, d.reason_summary)

    d = run(client=StubClient(answers(discards=0.74)))
    check("high restart_discards_state withholds the action", d.action == "none", d.action)
    check("that failure is named", names(d) == ["max_restart_discards_state"], str(names(d)))
    check("comparison is 'gt' for a maximum", d.failed_checks[0]["comparison"] == "gt")

    d = run(client=StubClient(answers(sev_conf=0.42)))
    check("low severity confidence withholds the action", d.action == "none", d.action)
    check("comparison is 'lt' for a minimum", d.failed_checks[0]["comparison"] == "lt")

    d = run(client=StubClient(answers(severity="not_an_incident", sev_conf=1.0)))
    check("a non-actionable severity withholds the action", d.action == "none", d.action)
    check("allowed_severities is what withheld it",
          names(d) == ["allowed_severities"], str(names(d)))
    check("reason_code says severity, not threshold", d.reason_code == "severity_not_permitted", d.reason_code)

    print("\npolicy wins over the model:")
    pol = policy(allowed_severities=["error"])
    d = run(pol=pol, client=StubClient(answers(severity="critical", sev_conf=1.0)))
    check("severity outside the policy's allowance is withheld", d.action == "none", d.action)
    check("the block is attributed to allowed_severities",
          any(c["check"] == "allowed_severities" for c in d.failed_checks), str(d.failed_checks))

    print("\nroute_only instead of escalate:")
    d = run(pol=policy(on_low_confidence="route_only"), client=StubClient(answers(help_=0.1)))
    check("action still withheld", d.action == "none")
    check("not marked escalated", d.escalated is False)
    check("the summary records route_only", d.reason_summary.startswith("route_only"), d.reason_summary)
    check("but the code stays a threshold failure", d.reason_code == "threshold_failed", d.reason_code)

    print("\nconfiguration states never produce an action:")
    cases = [
        ("gate disabled", envelope(), policy(enabled=False), "gate_disabled", False),
        ("no thresholds for the action", envelope(), policy(thresholds={}),
         "no_thresholds_declared", True),
        ("state_mode not declared", envelope(), policy(state_mode=None), "state_mode_not_declared", True),
    ]
    for label, ev, pol, expect, evaluated in cases:
        d = run(event=ev, pol=pol, client=StubClient())
        check(f"{label}: action none", d.action == "none", d.action)
        check(f"{label}: reason_code {expect}", d.reason_code == expect, d.reason_code)
        check(f"{label}: evaluated={evaluated}", d.evaluated is evaluated, str(d.evaluated))

    pol = policy()
    pol["auto_remediation"]["enabled"] = False
    d = run(pol=pol, client=StubClient())
    check("remediation disabled by policy: action none",
          d.action == "none" and d.reason_code == "remediation_not_enabled_by_policy", d.reason_code)

    pol = policy()
    pol["auto_remediation"]["target_action"] = "none"
    d = run(pol=pol, client=StubClient())
    check("target_action none: action none", d.action == "none")

    print("\nmodel alias is a configuration error, not a decision:")
    raised = False
    try:
        run(pol=policy(model="jev-latest"), client=StubClient())
    except dg.GateError:
        raised = True
    check("GateError is raised for an alias model", raised)

    print("\nevery failure class fails closed:")
    for name in ("TypeSafeAPITimeoutError", "TypeSafeAPIConnectionError", "TypeSafeRateLimitError",
                 "TypeSafeAuthenticationError", "TypeSafeBadRequestError"):
        d = run(client=StubClient(raises=err(name)))
        check(f"{name}: action none", d.action == "none", d.action)
        check(f"{name}: decision_unavailable", d.decision_unavailable is True)
        check(f"{name}: escalated for review", d.escalated is True)
        check(f"{name}: code is in the vocabulary", d.reason_code in dg.REASON_CODES, d.reason_code)
    d = run(client=StubClient(raises=err("TypeSafeAPITimeoutError")))
    check("timeout code is specific", d.reason_code == "gate_timeout", d.reason_code)
    d = run(client=StubClient(raises=err("TypeSafeBadRequestError")))
    check("bad request code is the bare code", d.reason_code == "gate_bad_request", d.reason_code)
    check("and the detail rides in the summary",
          "TypeSafeBadRequestError" in d.reason_summary, d.reason_summary)

    short = answers()
    del short["restart_discards_state"]
    d = run(client=StubClient(short))
    check("a missing answer fails closed", d.action == "none" and d.decision_unavailable,
          f"{d.action} / {d.reason_code}")
    check("missing answer code", d.reason_code == "missing_answer", d.reason_code)
    check("and the missing question is named in the summary",
          "restart_discards_state" in d.reason_summary, d.reason_summary)

    print("\nthe explanation is structured and bounded:")
    d = run(client=StubClient(answers(help_=0.4, discards=0.7, severity="warning", sev_conf=0.5)))
    check("every path yields a code from the vocabulary", d.reason_code in dg.REASON_CODES, d.reason_code)
    check("all four checks are recorded", len(d.failed_checks) == 4, str(names(d)))
    check("summary stays inside the bound",
          len(d.reason_summary) <= dg.MAX_REASON_SUMMARY, str(len(d.reason_summary)))
    # One failing check fits; four do not. That is the bound doing its job, and the
    # structured `failed_checks` above already carry the detail, so nothing is lost.
    single = run(client=StubClient(answers(help_=0.4)))
    check("a short explanation needs no pointer", single.reason_detail_ref is None,
          str(single.reason_detail_ref))
    check("four failing checks exceed the bound and set a pointer",
          d.reason_detail_ref is not None and d.reason_summary.endswith("..."),
          f"ref={d.reason_detail_ref} len={len(d.reason_summary)}")
    check("and the full prose is preserved for the ledger",
          d.reason_detail_full is not None and len(d.reason_detail_full) > dg.MAX_REASON_SUMMARY)
    check("while the structured checks stay complete",
          len(d.failed_checks) == 4, str(names(d)))
    check("envelope omits the unbounded prose", "reason_detail_full" not in d.envelope())
    check("the ledger record keeps it", "reason_detail_full" in d.to_dict())

    long_case = dg.Decision()
    dg._explain(long_case, "threshold_failed", detail="x" * 500)
    check("a long detail is truncated", long_case.reason_summary.endswith("..."),
          long_case.reason_summary[-20:])
    check("to the bound exactly", len(long_case.reason_summary) == dg.MAX_REASON_SUMMARY,
          str(len(long_case.reason_summary)))
    check("and the full text is preserved for the ledger",
          long_case.reason_detail_full is not None
          and len(long_case.reason_detail_full) > dg.MAX_REASON_SUMMARY)
    check("with a pointer instead of the prose",
          long_case.reason_detail_ref is not None
          and long_case.reason_detail_ref.startswith("artifact://sha256/"), str(long_case.reason_detail_ref))
    check("and the pointer is a content address, not a row number",
          long_case.reason_detail_ref == "artifact://sha256/"
          + hashlib.sha256((long_case.reason_detail_full or "").encode("utf-8")).hexdigest(),
          str(long_case.reason_detail_ref))

    raised = False
    try:
        dg._explain(dg.Decision(), "not_a_real_code")
    except dg.GateError:
        raised = True
    check("an invented reason code is rejected", raised)

    print("\ndecision ids:")
    a = run(client=StubClient())
    b = run(client=StubClient())
    check("identical state and thresholds give one id", a.decision_id == b.decision_id,
          f"{a.decision_id} vs {b.decision_id}")
    c = run(client=StubClient(), window=12345)
    check("a different window gives a different id", c.decision_id != a.decision_id)
    d_ = dg.evaluate(envelope(raw_message="panic: nil pointer"), policy(),
                     client=StubClient(answers(choice="runtime_panic")), schema=SCHEMA)
    check("a different fault class gives a different id", d_.decision_id != a.decision_id)

    print("\nno side effects:")
    source = Path(dg.__file__).read_text(encoding="utf-8")
    code = "\n".join(ln for ln in source.splitlines() if not ln.strip().startswith("#"))
    for forbidden in ("requests.", "subprocess", "os.system", "dispatch_", "popen", "urlopen"):
        check(f"no {forbidden} in the module", forbidden not in code, "found in source")
    check("evaluate returns a Decision and nothing else", isinstance(a, dg.Decision))
    check("Decision serialises with the new fields",
          set(a.to_dict()) >= {"action", "reason_code", "reason_summary", "raw_answers"})
    check("envelope is a strict subset of the record",
          set(a.envelope()) == set(a.to_dict()) - {"reason_detail_full"})

    if "--live" in sys.argv:
        print("\nlive end-to-end (real SDK, real schema, real API):")
        from typesafe_sdk import TypeSafeClient
        from windmill.flows.jev import loader

        class RecordingClient:
            """Delegates to the real client and keeps what was actually sent."""

            def __init__(self, inner):
                self.inner = inner
                self.sent: list = []

            def system_one(self, state=None, questions=None, model=None):
                self.sent.append(json.dumps(state))
                return self.inner.system_one(state=state, questions=questions, model=model)

        fixture = json.loads((ROOT / "tests" / "fixtures" / "events" /
                              "oom_memory_ceiling_01.json").read_text())
        real_schema = loader.load_schema(build=True)
        with TypeSafeClient() as real:
            recorder = RecordingClient(real)
            decision = dg.evaluate(fixture, policy(), client=recorder, schema=real_schema, window=1)

        print(f"    action={decision.action!r} code={decision.reason_code!r}")
        print(f"    summary={decision.reason_summary!r}")
        print(f"    checks={decision.failed_checks}")
        print(f"    model={decision.model_version} tokens={decision.usage} latency={decision.latency_ms}ms")
        check("live: a decision came back", decision.evaluated is True)
        check("live: exactly one request was sent", len(recorder.sent) == 1, str(len(recorder.sent)))
        sent_state = recorder.sent[0]
        check("live: the payload has the sanitized preview", "sanitized_preview" in sent_state)
        check("live: and no raw_message field", "raw_message" not in sent_state)
        check("live: the gate WITHHOLDS the restart", decision.action == "none", decision.action)
        check("live: and escalates instead", decision.escalated is True)
        check("live: the failing checks are structural",
              names(decision) == ["min_restart_would_help", "max_restart_discards_state"],
              str(names(decision)))
        check("live: summary within the bound",
              len(decision.reason_summary) <= dg.MAX_REASON_SUMMARY, str(len(decision.reason_summary)))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("all decision-gate checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
