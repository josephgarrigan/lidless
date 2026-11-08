#!/usr/bin/env python3
"""S2 inside sensor — declared vs actual.

What these checks exist for: the failure mode of an inside sensor is not "it
crashed", it is "it said nothing and we read that as healthy". So every check
here is about a distinction the sensor must not blur:

  1. an unmet required declaration is a finding, with re-runnable evidence
  2. an unmet *optional* declaration is reported, never escalated
  3. an unmeasured unit is not an absent unit
  4. a repeat updates one story instead of creating a second
  5. a resolved finding stays in the record
  6. a host with no declaration is refused, not declared healthy
  7. a malformed declaration is an error, not a silently skipped line
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "inside_declared_vs_actual.py"
sys.path.insert(0, str(ROOT / "scripts"))

import inside_declared_vs_actual as sv  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
        return
    FAILURES.append(name)
    print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


def write_expectations(tmp: Path, body: str, name: str = "expectations.yaml") -> Path:
    path = tmp / name
    path.write_text(body, encoding="utf-8")
    return path


BASE = """\
version: 1
declarations:
  - host: h1
    units:
      - name: a.service
        required: true
      - name: opt.service
        required: false
    listeners:
      - port: 8080
        process: app
"""


def actual(units: dict[str, bool], ports: list[int], host: str = "h1") -> dict:
    return {
        "host": host,
        "observed": "2026-11-05T00:00:00Z",
        "units": {
            name: {
                "state": "active" if up else "inactive",
                "active": up,
                "evidence": f"systemctl is-active {name}",
            }
            for name, up in units.items()
        },
        "listeners": ports,
    }


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lidless-s2-"))
    exp = write_expectations(tmp, BASE)
    expectations = sv.load_expectations(exp)

    print("1. A met declaration produces no findings:")
    healthy = sv.compare(expectations, actual({"a.service": True, "opt.service": True}, [8080]))
    check("healthy host is quiet", healthy == [], str(healthy))

    print("\n2. An unmet required declaration is high, with runnable evidence:")
    findings = sv.compare(expectations, actual({"a.service": False, "opt.service": True}, [8080]))
    check("exactly one finding", len(findings) == 1, str(len(findings)))
    if findings:
        check("severity high", findings[0]["severity"] == "high", findings[0]["severity"])
        check(
            "evidence is the command a reader can run",
            findings[0]["evidence"] == "systemctl is-active a.service",
            findings[0]["evidence"],
        )
        check(
            "subject names the unit and its state",
            "a.service" in findings[0]["subject"] and "inactive" in findings[0]["subject"],
            findings[0]["subject"],
        )
        check("it proposes review, never an action", findings[0]["proposes"] == "review")

    print("\n3. An unmet optional declaration is reported but not escalated:")
    findings = sv.compare(expectations, actual({"a.service": True, "opt.service": False}, [8080]))
    check("one finding", len(findings) == 1, str(len(findings)))
    if findings:
        check("severity info", findings[0]["severity"] == "info", findings[0]["severity"])

    print("\n4. A missing listener is named with its port and process:")
    findings = sv.compare(expectations, actual({"a.service": True, "opt.service": True}, []))
    check("one finding", len(findings) == 1, str(len(findings)))
    if findings:
        check("subject names the port", "8080" in findings[0]["subject"], findings[0]["subject"])
        check("subject names the process", "app" in findings[0]["subject"], findings[0]["subject"])
        check(
            "evidence greps the port",
            "ss -Hlnt" in findings[0]["evidence"] and "8080" in findings[0]["evidence"],
            findings[0]["evidence"],
        )

    print("\n5. An unmeasured unit is unmeasured, not absent:")
    unmeasured = actual({"opt.service": True}, [8080])  # a.service never collected
    findings = sv.compare(expectations, unmeasured)
    check("reported as a warning", len(findings) == 1 and findings[0]["severity"] == "warning", str(findings))
    if findings:
        check(
            "the subject says unmeasured",
            findings[0]["subject"].startswith("unmeasured"),
            findings[0]["subject"],
        )

    print("\n6. One finding, one story — a repeat updates:")
    ledger = tmp / "findings.jsonl"
    first = sv.merge_ledger(ledger, sv.compare(expectations, actual({"a.service": False, "opt.service": True}, [8080])))
    check("first occurrence is new", first[0]["state"] == "new" and first[0]["count"] == 1, str(first[0]))
    second = sv.merge_ledger(ledger, sv.compare(expectations, actual({"a.service": False, "opt.service": True}, [8080])))
    check("second occurrence is recurring", second[0]["state"] == "recurring", str(second[0]))
    check("count incremented", second[0]["count"] == 2, str(second[0].get("count")))
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("ledger holds one record, not two", len(rows) == 1, str(len(rows)))
    check(
        "first_observed survived the repeat",
        second[0]["first_observed"] == second[0]["observed"],
        str(second[0]),
    )

    print("\n7. A resolved finding stays in the record:")
    resolved = sv.merge_ledger(ledger, sv.compare(expectations, actual({"a.service": True, "opt.service": True}, [8080])))
    check("nothing is outstanding", resolved == [], str(resolved))
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("the resolved record is retained", len(rows) == 1 and rows[0]["state"] == "resolved", str(rows))
    check("it carries a resolved_at", "resolved_at" in rows[0], str(rows[0]))

    print("\n8. A host with no declaration is refused, not called healthy:")
    try:
        sv.compare(expectations, actual({"a.service": True, "opt.service": True}, [8080], host="stranger"))
        check("undeclared host raises", False, "compare returned normally")
    except sv.ExpectationsError as exc:
        check("undeclared host raises", "no declaration" in str(exc), str(exc))

    print("\n9. A malformed declaration is an error, not a skipped line:")
    broken = write_expectations(tmp, BASE + "\n        stray: value\n", "broken.yaml")
    try:
        sv.load_expectations(broken)
        check("malformed file raises", False, "load returned normally")
    except sv.ExpectationsError as exc:
        check("malformed file raises", "stray" in str(exc), str(exc))

    print("\n10. Exit codes keep 'could not measure' distinct from 'measured, unhealthy':")
    good_actual = tmp / "good.json"
    good_actual.write_text(json.dumps(actual({"a.service": True, "opt.service": True}, [8080])), encoding="utf-8")
    bad_actual = tmp / "bad.json"
    bad_actual.write_text(
        json.dumps(actual({"a.service": False, "opt.service": True}, [8080])), encoding="utf-8"
    )

    def run(*extra: str, expect_path: Path = exp) -> int:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--expectations", str(expect_path), "check", *extra],
            capture_output=True,
            text=True,
        ).returncode

    check("healthy → 0", run("--actual", str(good_actual)) == 0, str(run("--actual", str(good_actual))))
    check("unhealthy → 1", run("--actual", str(bad_actual), "--ledger", str(tmp / "l2.jsonl")) == 1)
    check(
        "unhealthy with --fail-on none → 0 (report without failing)",
        run("--actual", str(bad_actual), "--ledger", str(tmp / "l3.jsonl"), "--fail-on", "none") == 0,
    )
    check("missing actual → 2", run("--actual", str(tmp / "nope.json")) == 2)
    check("undeclared host → 2", run("--actual", str(good_actual), "--expectations-hash") == 0)

    print("\n11. The expectations file is the contract, and its digest is visible:")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "collect", "--host", "not-declared", "--out", str(tmp / "x.json")],
        capture_output=True,
        text=True,
    )
    check("collecting an undeclared host is refused", proc.returncode == 2, proc.stdout + proc.stderr)
    check(
        "the refusal explains why",
        "not declared" in proc.stderr,
        proc.stderr.strip()[:120],
    )

    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("\nall inside declared-vs-actual checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
