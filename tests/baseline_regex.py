#!/usr/bin/env python3
"""L-001 baseline: run the CURRENT regex engine + router over the fixture corpus.

Exercises the real code, not a reimplementation: imports `windmill.flows.anecdote_engine`
and `windmill.flows.tenant_router`, and calls `tenant_router.main()` with the two
dispatch functions stubbed so nothing leaves the process.

Writes tests/out/baseline.json and prints per-class agreement plus the divergences
that later stories are meant to fix.

Usage:
    python3 tests/baseline_regex.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TENANT_POLICY_DIR", str(ROOT / "rules" / "tenants"))

import yaml  # noqa: E402

from windmill.flows import anecdote_engine as ae  # noqa: E402
from windmill.flows import tenant_router as tr  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "events"
LABELS = ROOT / "tests" / "fixtures" / "labels.yaml"
OUT = ROOT / "tests" / "out" / "baseline.json"

HEADLINE_SEV = re.compile(r"^\[([A-Z]+)\]")
KNOWN_CLASSES = {"DB_CONNECTION_EXHAUSTION", "OOM_MEMORY_CEILING", "AUTH_SECURITY_REJECTION",
                 "UPSTREAM_TIMEOUT", "RUNTIME_PANIC"}


def silence_dispatch():
    """Replace the router's outbound sinks with recorders. Nothing leaves the process."""
    tr.dispatch_matrix_anecdote = lambda *a, **k: None  # type: ignore[assignment]
    tr.dispatch_slack_anecdote = lambda *a, **k: None  # type: ignore[assignment]


def load_labels() -> dict[str, dict]:
    data = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
    return {entry["fixture"]: entry for entry in data["labels"]}


def analyse(payload: dict, label: dict) -> dict:
    policy = tr.load_tenant_policy(payload["tenant_id"])
    verbosity = policy.get("anecdote_verbosity", "user_friendly")
    anecdote = ae.synthesize_anecdote(payload, verbosity=verbosity)

    headline_sev = HEADLINE_SEV.match(anecdote["headline"])
    engine_severity = headline_sev.group(1) if headline_sev else "UNKNOWN"
    envelope_severity = payload.get("severity", "error")

    # The router is called for EVERY event, benign included: it has no is_error guard,
    # so non-error log lines are dispatched like any other. An earlier version of this
    # harness skipped benign events, which hid that behaviour (issue #8).
    routed = tr.main(payload)
    dispatched = routed.get("dispatched_to", [])
    remediation = [d for d in dispatched if d.startswith("triggered_remediation_")]

    engine_class = anecdote["anecdote_id"]
    # Any matched pattern id that is not one of the five is the generic fallback.
    matched_real_pattern = engine_class in KNOWN_CLASSES
    misresolved = (
        "multi_pattern" in label["tags"]
        and matched_real_pattern
        and engine_class != label["fault_class"]
    )

    return {
        "fixture": label["fixture"],
        "tags": label["tags"],
        "label_class": label["fault_class"],
        "engine_class": engine_class,
        "generic_fallback": not matched_real_pattern,
        "class_match": engine_class == label["fault_class"],
        "misresolved_by_first_match": misresolved,
        "label_severity_band": label["severity_band"],
        "engine_severity": engine_severity,
        "envelope_severity": envelope_severity,
        "severity_diverges": engine_severity.lower() != envelope_severity.lower(),
        "tenant": payload["tenant_id"],
        "remediation_policy_enabled": bool(policy.get("auto_remediation", {}).get("enabled", False)),
        "dispatched_to": dispatched,
        "remediation_triggered": bool(remediation),
        "remediation_expected": label["remediation_expected"],
        "remediation_agrees": bool(remediation) == label["remediation_expected"],
        "remediation_mismatch_cause": _cause(bool(remediation), label["remediation_expected"],
                                             policy, envelope_severity),
        "action_required": anecdote["action_required"],
        "action_required_used_by_router": False,
        "rationale": label["rationale"],
    }


def _cause(triggered: bool, expected: bool, policy: dict, envelope_severity: str) -> str:
    """Why the current rule and the human label disagree, in one word."""
    if triggered == expected:
        return "agree"
    if not policy.get("auto_remediation", {}).get("enabled", False):
        return "policy_disabled_for_tenant"
    if triggered and not expected:
        return "fired_on_severity_string_alone"
    if envelope_severity != "critical":
        return "severity_below_critical_floor"
    return "other"


def main() -> int:
    silence_dispatch()
    labels = load_labels()
    rows = []
    for path in sorted(FIXTURES.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        fid = payload.get("fixture_id", path.stem)
        if fid not in labels:
            print(f"  ! no label for {fid}")
            continue
        rows.append(analyse(payload, labels[fid]))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"fixtures": len(rows), "rows": rows}, indent=2), encoding="utf-8")

    # Fixtures whose label is a judgement call are marked AMBIGUOUS (issue #7 ruling):
    # they are excluded from every accuracy figure, and kept only for the
    # first-match-wins ordering test, which is what they were built for.
    all_errors = [r for r in rows if "benign" not in r["tags"]]
    ambiguous = [r for r in all_errors if "ambiguous" in r["tags"]]
    errors = [r for r in all_errors if "ambiguous" not in r["tags"]]
    benign = [r for r in rows if "benign" in r["tags"]]

    print(f"\nfixtures: {len(rows)}  (error events {len(errors)}, benign {len(benign)})")

    print("\ntenant policy resolution (indexed by declared tenant_id, issue #6 fix):")
    policy_dir = Path(os.environ["TENANT_POLICY_DIR"])
    for tenant in sorted({r["tenant"] for r in rows}):
        path, resolved = tr.resolve_policy_source(tenant)
        enabled = tr.load_tenant_policy(tenant).get("auto_remediation", {}).get("enabled", False)
        note = "OK" if resolved else f"UNRESOLVED -> fallback to {tr.FALLBACK_TENANT_ID}"
        print(f"  tenant {tenant!r}: {os.path.basename(path)} [{note}] auto_remediation.enabled={enabled}")

    print("\nper-class agreement (label -> engine):")
    classes = sorted({r["label_class"] for r in errors})
    for cls in classes:
        subset = [r for r in errors if r["label_class"] == cls]
        hits = sum(1 for r in subset if r["class_match"])
        print(f"  {cls:<26} {hits}/{len(subset)}")

    print(f"\nfirst-match-wins ordering test on the AMBIGUOUS set ({len(ambiguous)} fixtures):")
    for r in ambiguous:
        intended = labels[r["fixture"]].get("intended_class")
        verdict = "OK" if r["engine_class"] == intended else "MISRESOLVED"
        print(f"  {r['fixture']:<24} intended={intended:<26} engine={r['engine_class']:<26} {verdict}")
    mis = [r for r in ambiguous if r["engine_class"] != labels[r["fixture"]].get("intended_class")]
    print(f"  misresolved by ordering: {len(mis)}/{len(ambiguous)}"
          "  (excluded from accuracy figures - the labels are judgement calls)")

    novel = [r for r in errors if "novel" in r["tags"]]
    print(f"\nnovel faults swallowed by the generic fallback: "
          f"{sum(1 for r in novel if r['generic_fallback'])}/{len(novel)}")
    for r in novel:
        print(f"  {r['fixture']:<24} -> {r['engine_class']} (severity {r['engine_severity']})")

    print(f"\nremediation agreement: {sum(1 for r in errors if r['remediation_agrees'])}/{len(errors)}")
    causes: dict[str, int] = {}
    for r in errors:
        causes[r["remediation_mismatch_cause"]] = causes.get(r["remediation_mismatch_cause"], 0) + 1
    print("  by cause:", ", ".join(f"{k}={v}" for k, v in sorted(causes.items())))
    live = [r for r in errors if r["remediation_policy_enabled"]]
    print(f"  restricted to tenants with auto_remediation enabled "
          f"({sorted({r['tenant'] for r in live})}): "
          f"{sum(1 for r in live if r['remediation_agrees'])}/{len(live)} agree")
    for cause in ("fired_on_severity_string_alone", "severity_below_critical_floor", "other"):
        for r in [r for r in errors if r["remediation_mismatch_cause"] == cause]:
            print(f"  {cause:<32} {r['fixture']:<26} envelope_sev={r['envelope_severity']} "
                  f"expected={r['remediation_expected']}")

    div = [r for r in rows if "benign" not in r["tags"] and r["severity_diverges"]]
    print(f"\nengine severity vs envelope severity diverges on {len(div)}/{len(errors)} error events")
    print("  (routing and the remediation gate both read the ENVELOPE severity; the engine's"
          "\n   severity only appears in narration, so the semantic severity has no effect on actions)")

    print(f"\nwrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
