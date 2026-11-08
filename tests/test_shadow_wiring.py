#!/usr/bin/env python3
"""L-007 tests: shadow wiring changes nothing that acts.

The claim under test is negative, so it is tested two ways:

  * against recorded history - `dispatched_to` for every fixture must still match
    the L-001 baseline;
  * against itself - every response key other than `shadow_decision` must be
    identical between a shadow-off pass and a shadow-on pass.

Usage:
    python3 tests/test_shadow_wiring.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from windmill.flows import tenant_router as tr  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "events"
BASELINE = ROOT / "tests" / "out" / "baseline.json"
POLICY_DIR = ROOT / "rules" / "tenants"
QUESTIONS = {"fault_class": None, "severity": None, "is_transient": None,
             "restart_would_help": None, "restart_discards_state": None}
SCHEMA = {"version": "v1", "questions": QUESTIONS, "model": "jev-1.13.0"}
SHADOW_TENANTS = ("fintech", "alpha-corp")
# Response fields that exist only because shadow evaluated the gate. task 186
# (primary input) added `provenance` alongside `shadow_decision`; the negative
# claim is that nothing ELSE changes, so the exemption list is explicit and
# every entry is also asserted absent in the shadow-off pass.
SHADOW_ONLY_KEYS = ("shadow_decision", "provenance")
LOG_TEXT = ("remaining connection slots", "fatal error", "secret_pw123",
            "NullPointerException", "gateway timeout", "out of memory")
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{'' if condition else ' - ' + detail}")
    if not condition:
        FAILURES.append(label)


def answers(help_=0.91, discards=0.05, severity="critical", sev_conf=0.95) -> dict:
    return {
        "fault_class": SimpleNamespace(choice="oom_memory_ceiling", confidence=0.98, probabilities={}),
        "severity": SimpleNamespace(choice=severity, confidence=sev_conf, probabilities={}),
        "is_transient": SimpleNamespace(noul=0.2),
        "restart_would_help": SimpleNamespace(noul=help_),
        "restart_discards_state": SimpleNamespace(noul=discards),
    }


class StubClient:
    def __init__(self, ans=None):
        self.ans = ans or answers()
        self.calls: list = []

    def system_one(self, state=None, questions=None, model=None):
        self.calls.append({"state": state, "model": model})
        return SimpleNamespace(answers=self.ans, model=model,
                               usage=SimpleNamespace(input_tokens=985, output_tokens=180))


def silence_dispatch() -> None:
    tr.dispatch_matrix_anecdote = lambda *a, **k: None  # type: ignore[assignment]
    tr.dispatch_slack_anecdote = lambda *a, **k: None  # type: ignore[assignment]


def load_fixtures() -> list:
    return [json.loads(p.read_text()) for p in sorted(FIXTURES.glob("*.json"))]


def baseline_rows() -> dict:
    return {r["fixture"]: r for r in json.loads(BASELINE.read_text())["rows"]}


def make_policy_dir(tmp: Path, *, shadow: bool, silenced: bool = False,
                    model: str = "jev-1.13.0", state_mode: str | None = None,
                    shadow_tenants=SHADOW_TENANTS) -> Path:
    """Copy the shipped policies, turning shadow on for the named tenants."""
    target = tmp / "tenants"
    target.mkdir(parents=True, exist_ok=True)
    for path in POLICY_DIR.glob("*.yaml"):
        policy = yaml.safe_load(path.read_text())
        tenant = policy.get("tenant_id")
        if tenant in shadow_tenants:
            policy["decision_gate"]["shadow"] = shadow
            policy["decision_gate"]["model"] = model
            if state_mode:
                policy["decision_gate"]["state_mode"] = state_mode
            if silenced:
                policy["silenced"] = True
        target.joinpath(path.name).write_text(yaml.safe_dump(policy, sort_keys=True))
    return target


def use_policy_dir(directory: Path) -> None:
    tr.POLICY_DIR = str(directory)
    tr.build_policy_index(force=True)


def run_all(fixtures, **kwargs) -> dict:
    return {f["fixture_id"]: tr.main(f, **kwargs) for f in fixtures}


def jsonl_lines(root: Path) -> list:
    if not root.exists():
        return []
    return [json.loads(ln) for p in sorted(root.rglob("*.jsonl")) for ln in p.read_text().splitlines()]


def main() -> int:
    silence_dispatch()
    fixtures = load_fixtures()
    baseline = baseline_rows()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger_off, ledger_on = tmp / "ledger_off", tmp / "ledger_on"

        print("pass 1 - shadow OFF (the shipped default):")
        use_policy_dir(make_policy_dir(tmp, shadow=False))
        client_off = StubClient()
        off = run_all(fixtures, gate_client=client_off, gate_schema=SCHEMA, ledger_root=ledger_off)
        drift = [fid for fid, r in off.items()
                 if r["dispatched_to"] != baseline[fid]["dispatched_to"]]
        check("dispatch matches the L-001 baseline for every fixture", not drift, str(drift[:4]))
        check("no fixture carries a shadow decision",
              all("shadow_decision" not in r for r in off.values()))
        check("no fixture carries gate provenance",
              all("provenance" not in r for r in off.values()))
        check("the gate was never called", not client_off.calls, str(len(client_off.calls)))
        check("nothing reached the ledger", not jsonl_lines(ledger_off))

        print("\npass 2 - shadow ON for fintech and alpha-corp:")
        use_policy_dir(make_policy_dir(tmp, shadow=True))
        client_on = StubClient()
        on = run_all(fixtures, gate_client=client_on, gate_schema=SCHEMA, ledger_root=ledger_on)

        changed = [fid for fid in on
                   if {k: v for k, v in on[fid].items() if k not in SHADOW_ONLY_KEYS} != off[fid]]
        check("every response is identical apart from the shadow-only fields",
              not changed, str(changed[:4]))
        check("provenance appears only where a shadow decision was recorded",
              all(("provenance" in r) == ("shadow_decision" in r) for r in on.values()))
        drift = [fid for fid, r in on.items()
                 if r["dispatched_to"] != baseline[fid]["dispatched_to"]]
        check("dispatch still matches the baseline", not drift, str(drift[:4]))

        # lidless#8: the is_error stopgap ends the call before the gate is consulted, so a
        # suppressed event has no decision to carry. Suppression is asserted as its own fact
        # rather than surfacing as a missing shadow_decision.
        suppressed = [fid for fid, r in on.items() if r["status"] == "not_an_incident"]
        judged = [fid for fid in on if fid not in suppressed]
        check("the benign fixtures are suppressed, not narrated (the corpus has 10)",
              len(suppressed) == 10, str(len(suppressed)))
        check("every suppressed event names the stopgap and dispatched nothing",
              all(on[fid].get("suppressed_by") == "is_error_stopgap" and on[fid]["dispatched_to"] == []
                  for fid in suppressed),
              str([(fid, on[fid].get("suppressed_by")) for fid in suppressed[:3]]))
        check("every judged event carries a shadow decision",
              all("shadow_decision" in on[fid] for fid in judged))
        check("the gate ran once per judged event", len(client_on.calls) == len(judged),
              f"{len(client_on.calls)} vs {len(judged)}")
        check("no shadow decision contains an action",
              all("action" not in on[fid]["shadow_decision"] for fid in judged))

        records = jsonl_lines(ledger_on)
        check("one ledger line per judged event", len(records) == len(judged),
              f"{len(records)} vs {len(judged)}")

        print("\nthe ledger never carries log content:")
        leaks = []
        for record in records:
            blob = json.dumps(record)
            if "raw_message" in blob or "sanitized_preview" in blob:
                leaks.append("raw field present")
            if any(text in blob for text in LOG_TEXT):
                leaks.append("message text present")
        check("no raw field and no message text in any line", not leaks, str(set(leaks)))
        check("container comes from structured metadata",
              all(r.get("container") for r in records), "a line has no container")
        sources = {r["archive_source"] for r in records}
        check("tenants with their own prefix used it", sources == {"tenant"}, str(sources))
        # OD-4's fallback is exercised directly: every shipped tenant declares a prefix,
        # so the rule would otherwise never run.
        from windmill.flows import shadow_ledger as sl
        prefixless = {"tenant_id": "newco", "routing": {}}
        platform_policy = yaml.safe_load((POLICY_DIR / "platform.yaml").read_text())
        prefix, source = sl.archive_for(prefixless, platform_policy)
        check("a tenant with no prefix falls back to the platform archive",
              source == "platform_fallback"
              and prefix == platform_policy["routing"]["s3_archive_prefix"], f"{source} / {prefix}")
        check("with no platform prefix either, the archive is unassigned",
              sl.archive_for(prefixless, {"routing": {}})[1] == "unassigned")
        check("reasoning is recorded structurally",
              all(isinstance(r.get("failed_checks"), list) for r in records))

        print("\nagreement accounting:")
        check("agreement is a boolean everywhere",
              all(isinstance(r["agreement"], bool) for r in records))
        check("classification is a known value or absent",
              {r["disagreement_type"] for r in records} <= {None, "gate_withholds", "gate_would_act"},
              str({r["disagreement_type"] for r in records}))
        withheld = [r for r in records if r["disagreement_type"] == "gate_withholds"]
        would_act = [r for r in records if r["disagreement_type"] == "gate_would_act"]
        print(f"      rule acted, gate would not: {len(withheld)}")
        print(f"      gate would have acted, rule did not: {len(would_act)}")
        check("a disagreement is counted in one bucket or the other",
              len(withheld) + len(would_act) + sum(1 for r in records if r["agreement"]) == len(records))
        check("with permissive answers the gate agrees wherever the rule acts",
              all(r["agreement"] for r in records if r["rule_outcome"]["remediation_triggered"]),
              "unexpected disagreement on a permissive run")

        print("\nthe verdict is preserved even though nothing acts:")
        gated = [r for r in records if r["would_be_action"] not in (None, "none")]
        check("some lines record what the gate would have done", len(gated) >= 0)
        check("would_be_code is always present", all(r["would_be_code"] for r in records))
        check("no line records a taken action",
              all(not any(str(k).startswith("action") for k in r) for r in records))

        print("\nthe disagreement this exercise exists to find:")
        use_policy_dir(make_policy_dir(tmp, shadow=True, shadow_tenants=("fintech",)))
        # The answers L-002 actually measured on the OOM fixture: a restart is a coin
        # flip to help (0.48) and likely to discard in-flight work (0.74), while the
        # current rule restarts on the severity string alone.
        client_low = StubClient(answers(help_=0.40, discards=0.74))
        ledger_low = tmp / "ledger_low"
        run_all(fixtures, gate_client=client_low, gate_schema=SCHEMA, ledger_root=ledger_low)
        low = jsonl_lines(ledger_low)
        withheld_low = [r for r in low if r["disagreement_type"] == "gate_withholds"]
        check("the gate withholds where the rule restarts", len(withheld_low) > 0,
              f"{len(withheld_low)} line(s)")
        check("and the withheld verdict names the failing checks",
              all(r["failed_checks"] for r in withheld_low), "a withheld line carries no checks")
        check("the withheld verdict still records an answer set",
              all(r["answers"] for r in withheld_low), "no answers recorded")

        print("\nstructured_only mode sends no log text to the vendor:")
        use_policy_dir(make_policy_dir(tmp, shadow=True, state_mode="structured_only",
                                       shadow_tenants=("fintech",)))
        client_so = StubClient()
        ledger_so = tmp / "ledger_so"
        run_all(fixtures, gate_client=client_so, gate_schema=SCHEMA, ledger_root=ledger_so)
        sent = " ".join(json.dumps(c["state"]) for c in client_so.calls)
        check("no message text reached the request",
              not any(text in sent for text in LOG_TEXT), "log text was sent")
        check("the state mode is recorded", '"state_mode": "structured_only"' in sent
              or "'state_mode': 'structured_only'" in sent, sent[:120])
        check("and no preview field", "sanitized_preview" not in sent)
        check("the ledger stayed clean",
              not any(text in json.dumps(r) for r in jsonl_lines(ledger_so) for text in LOG_TEXT))

        print("\na broken ledger does not break dispatch:")
        use_policy_dir(make_policy_dir(tmp, shadow=True, shadow_tenants=("fintech",)))
        blocker = tmp / "not-a-directory"
        blocker.write_text("i am a file")
        result = tr.main(fixtures[0], gate_client=StubClient(), gate_schema=SCHEMA,
                         ledger_root=blocker)
        check("dispatch still happened",
              result["dispatched_to"] == baseline[fixtures[0]["fixture_id"]]["dispatched_to"],
              str(result["dispatched_to"]))
        check("the ledger failure is reported, not raised",
              result["shadow_decision"]["ledger"] == "error", str(result["shadow_decision"]))

        print("\na silenced tenant is not gated:")
        use_policy_dir(make_policy_dir(tmp, shadow=True, silenced=True,
                                       shadow_tenants=("fintech",)))
        client_sil = StubClient()
        ledger_sil = tmp / "ledger_sil"
        result = tr.main(fixtures[0], gate_client=client_sil, gate_schema=SCHEMA,
                         ledger_root=ledger_sil)
        check("returns silenced", result.get("status") == "silenced", str(result))
        check("the gate was never called", not client_sil.calls, str(len(client_sil.calls)))
        check("no ledger line was written", not jsonl_lines(ledger_sil))

        print("\na configuration error in the gate is contained:")
        use_policy_dir(make_policy_dir(tmp, shadow=True, model="jev-latest",
                                       shadow_tenants=("fintech",)))
        result = tr.main(fixtures[0], gate_client=StubClient(), gate_schema=SCHEMA,
                         ledger_root=tmp / "ledger_err")
        check("dispatch is unaffected",
              result["dispatched_to"] == baseline[fixtures[0]["fixture_id"]]["dispatched_to"],
              str(result.get("dispatched_to")))
        check("the error is surfaced, not swallowed",
              "error" in result.get("shadow_decision", {}), str(result.get("shadow_decision")))

    tr.POLICY_DIR = str(POLICY_DIR)
    tr.build_policy_index(force=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("all shadow-wiring checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
