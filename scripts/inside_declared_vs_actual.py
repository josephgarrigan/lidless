#!/usr/bin/env python3
"""S2 — declared vs actual.

The class of finding this exists for is task 323: VictoriaLogs was not listening
and nothing said so, because "nothing is wrong in the logs" and "nothing is
arriving" look identical from the centre. A human noticed. This measures the
declaration instead.

Two subcommands, deliberately separate:

    collect   runs ON the host, writes what it measured as JSON
    check     runs anywhere, diffs a collection against `rules/inside/expectations.yaml`

The split is rule 4 of the inside proposal: no sensor reads another host's
filesystem or asks another host to run a command on its behalf. The host reports
on itself through its own collector; the sensor only compares.

Every finding carries an `evidence` string that a reader can run in five minutes
and get the same answer. A finding whose evidence cannot be re-run is not
admissible, so the evidence field is generated from the same command the
collector executed, not written by hand.

Lidless proposes; it never acts. There is no remediation path in this file, by
design, and exit code 1 is a report — not a trigger.

Usage:
    # on the host
    python3 scripts/inside_declared_vs_actual.py collect \
        --host media-node --out /var/lib/lidless/actual.json

    # anywhere
    python3 scripts/inside_declared_vs_actual.py check \
        --expectations rules/inside/expectations.yaml \
        --actual /var/lib/lidless/actual.json \
        --ledger /var/lib/lidless/findings.jsonl

Exit codes:
    0  every required declaration is met
    1  at least one unmet declaration (findings were emitted)
    2  the measurement itself failed (unreadable expectation, bad collection)
       — a distinct fault: never report "healthy" when you could not measure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOST_KEYS = {"host"}
UNIT_KEYS = {"name", "required"}
LISTENER_KEYS = {"port", "process"}

SEVERITY = {
    # A required unit that is not active, or a declared listener that is absent,
    # is a `high` finding: the host is not what it says it is. An unrequired
    # unit's absence is `info` — reported, never escalated.
    "required-absent": "high",
    "listener-absent": "high",
    "optional-absent": "info",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Parsing the expectation file without pyyaml
# ---------------------------------------------------------------------------
# The runtime modules in this repo are stdlib-only by design. requirements-dev
# carries pyyaml for the *tests*; the sensor must run on a host that has nothing
# installed. So this is a deliberately small reader for the subset this file
# uses: `key: value`, lists of maps under `units:`/`listeners:`, comments.
#
# It refuses rather than guesses: an indentation it does not understand is an
# error, because a silently-skipped declaration is exactly the false quiet this
# sensor exists to remove.


class ExpectationsError(ValueError):
    pass


def _scalar(text: str) -> Any:
    text = text.strip()
    if text in ("true", "false"):
        return text == "true"
    if text.isdigit():
        return int(text)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def load_expectations(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ExpectationsError(f"expectations not found: {path}")

    version = None
    hosts: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    section: str | None = None  # "units" | "listeners"
    item: dict[str, Any] | None = None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()

        if indent == 0:
            key, _, value = body.partition(":")
            if key == "version":
                version = _scalar(value)
            elif key == "declarations":
                continue
            else:
                raise ExpectationsError(f"unknown top-level key: {key}")
            continue

        if indent == 2 and body.startswith("- "):
            current = {}
            hosts.append(current)
            section, item = None, None
            key, _, value = body[2:].partition(":")
            current[key.strip()] = _scalar(value)
            continue

        if current is None:
            raise ExpectationsError("content before the first declaration")

        if indent == 4:
            key, _, value = body.partition(":")
            key = key.strip()
            if key in ("units", "listeners"):
                section, item = key, None
                current.setdefault(key, [])
            elif key in HOST_KEYS:
                current[key] = _scalar(value)
                section, item = None, None
            else:
                raise ExpectationsError(f"unknown declaration key: {key}")
            continue

        if indent == 6 and body.startswith("- ") and section:
            item = {}
            current[section].append(item)
            key, _, value = body[2:].partition(":")
            key = key.strip()
            allowed = UNIT_KEYS if section == "units" else LISTENER_KEYS
            if key not in allowed:
                raise ExpectationsError(f"unknown {section} key: {key}")
            item[key] = _scalar(value)
            continue

        if indent == 8 and section and item is not None:
            key, _, value = body.partition(":")
            key = key.strip()
            allowed = UNIT_KEYS if section == "units" else LISTENER_KEYS
            if key not in allowed:
                raise ExpectationsError(f"unknown {section} key: {key}")
            item[key] = _scalar(value)
            continue

        raise ExpectationsError(f"unparseable line at indent {indent}: {body!r}")

    if version != 1:
        raise ExpectationsError(f"unsupported expectations version: {version!r}")

    seen: set[str] = set()
    for host in hosts:
        name = host.get("host")
        if not name:
            raise ExpectationsError("a declaration is missing `host`")
        if name in seen:
            raise ExpectationsError(f"duplicate host declaration: {name}")
        seen.add(name)
        for unit in host.get("units", []):
            if "name" not in unit:
                raise ExpectationsError(f"{name}: unit without `name`")
            unit.setdefault("required", True)
        for listener in host.get("listeners", []):
            if "port" not in listener:
                raise ExpectationsError(f"{name}: listener without `port`")
    return {"version": version, "declarations": hosts}


# ---------------------------------------------------------------------------
# collect — runs on the host
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - host dependent
        return 127, "", str(exc)


def _listening_ports() -> tuple[list[int], str]:
    """TCP listeners, from `ss`. The command is the evidence, so it is returned."""
    code, out, err = _run(["ss", "-Hlnt"])
    if code != 0:
        raise RuntimeError(f"ss failed ({code}): {err.strip()[:200]}")
    ports: set[int] = set()
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        local = fields[3]
        _, _, port = local.rpartition(":")
        if port.isdigit():
            ports.add(int(port))
    return sorted(ports), "ss -Hlnt"


def collect(host: str, unit_names: list[str]) -> dict[str, Any]:
    units: dict[str, Any] = {}
    for name in unit_names:
        code, out, _ = _run(["systemctl", "is-active", name])
        state = out.strip().splitlines()[0] if out.strip() else "unknown"
        # `is-active` returns 0 only for "active".
        units[name] = {
            "state": state,
            "active": code == 0 and state == "active",
            "evidence": f"systemctl is-active {name}",
        }

    ports, listener_evidence = _listening_ports()
    return {
        "host": host,
        "observed": utc_now(),
        "collector": "scripts/inside_declared_vs_actual.py collect",
        "units": units,
        "listeners": ports,
        "evidence": {
            "listeners": listener_evidence,
            "units": "systemctl is-active <unit>",
        },
    }


# ---------------------------------------------------------------------------
# check — diffs a collection against declarations
# ---------------------------------------------------------------------------


def fingerprint(source: str, subject: str, evidence: str) -> str:
    """The identity of a finding, exactly as curator_intake dedupes on it."""
    return hashlib.sha256(f"{source}|{subject}|{evidence}".encode("utf-8")).hexdigest()


def _finding(
    *,
    source: str,
    subject: str,
    evidence: str,
    severity: str,
    observed: str,
    kind: str,
) -> dict[str, Any]:
    return {
        "source": source,
        "subject": subject,
        "evidence": evidence,
        "severity": severity,
        "observed": observed,
        "kind": kind,
        "fingerprint": fingerprint(source, subject, evidence),
        "story_label": f"lidless:{fingerprint(source, subject, evidence)[:16]}",
        "proposes": "review",           # never an action
        "admitted_by": "declaration",   # S2 admits on the declaration itself
    }


def compare(expectations: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    host = actual.get("host")
    declared = next(
        (d for d in expectations["declarations"] if d["host"] == host), None
    )
    if declared is None:
        raise ExpectationsError(
            f"host {host!r} has a collection but no declaration — refusing to "
            "report it as healthy"
        )

    observed = actual.get("observed") or utc_now()
    source = f"host:{host}"
    measured_units: dict[str, Any] = actual.get("units", {})
    measured_ports = set(actual.get("listeners", []))

    findings: list[dict[str, Any]] = []

    for unit in declared.get("units", []):
        name = unit["name"]
        required = unit.get("required", True)
        measured = measured_units.get(name)
        # A unit that was never measured is not "absent" — it is unmeasured, and
        # saying otherwise would turn a collector bug into a host finding.
        if measured is None:
            findings.append(
                _finding(
                    source=source,
                    subject=f"unmeasured unit: {name}",
                    evidence=f"systemctl is-active {name}",
                    severity="warning",
                    observed=observed,
                    kind="required-absent",
                )
            )
            continue
        if measured.get("active"):
            continue
        kind = "required-absent" if required else "optional-absent"
        findings.append(
            _finding(
                source=source,
                subject=f"declared unit not active: {name} ({measured.get('state', 'unknown')})",
                evidence=measured.get("evidence") or f"systemctl is-active {name}",
                severity=SEVERITY[kind],
                observed=observed,
                kind=kind,
            )
        )

    for listener in declared.get("listeners", []):
        port = int(listener["port"])
        if port in measured_ports:
            continue
        findings.append(
            _finding(
                source=source,
                subject=f"declared listener not present: tcp/{port} ({listener.get('process', 'unnamed')})",
                evidence=f"ss -Hlnt | grep ':{port} '  # then: systemctl status {listener.get('process', '')}".strip(),
                severity=SEVERITY["listener-absent"],
                observed=observed,
                kind="listener-absent",
            )
        )

    return findings


def merge_ledger(ledger_path: Path | None, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One finding, one story: a repeat updates, it does not multiply.

    The ledger is JSONL, append-only in spirit: we read it, merge by fingerprint,
    and rewrite it. Rewriting is the one thing this file does to stored state,
    and it is why the ledger is a file the sensor owns — not the story matrix.
    """
    existing: dict[str, dict[str, Any]] = {}
    if ledger_path and ledger_path.is_file():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line is not a reason to lose the rest
            if "fingerprint" in record:
                existing[record["fingerprint"]] = record

    merged: list[dict[str, Any]] = []
    for finding in findings:
        prior = existing.pop(finding["fingerprint"], None)
        if prior:
            finding["first_observed"] = prior.get("first_observed", prior.get("observed"))
            finding["count"] = int(prior.get("count", 1)) + 1
            finding["state"] = "recurring"
        else:
            finding["first_observed"] = finding["observed"]
            finding["count"] = 1
            finding["state"] = "new"
        merged.append(finding)

    # Findings that were present before and are not present now are resolved,
    # not deleted: the record of a condition that ended is evidence too.
    resolved = [
        {**record, "state": "resolved", "resolved_at": utc_now()}
        for record in existing.values()
        if record.get("state") != "resolved"
    ]

    if ledger_path:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        rows = merged + resolved + [
            r for r in existing.values() if r.get("state") == "resolved"
        ]
        ledger_path.write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in rows) + ("\n" if rows else ""),
            encoding="utf-8",
        )
    return merged


def cmd_collect(args: argparse.Namespace) -> int:
    expectations = load_expectations(Path(args.expectations))
    declared = next(
        (d for d in expectations["declarations"] if d["host"] == args.host), None
    )
    if declared is None:
        print(
            f"refusing: {args.host!r} is not declared in {args.expectations}. "
            "Add the declaration first; the collector measures a contract, it does not invent one.",
            file=sys.stderr,
        )
        return 2

    units = [u["name"] for u in declared.get("units", [])]
    try:
        payload = collect(args.host, units)
    except RuntimeError as exc:
        print(f"collection failed: {exc}", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"{args.host}: {sum(1 for u in payload['units'].values() if u['active'])}"
        f"/{len(payload['units'])} declared units active; "
        f"{len(payload['listeners'])} TCP listeners; wrote {out}"
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    try:
        expectations = load_expectations(Path(args.expectations))
        actual = json.loads(Path(args.actual).read_text(encoding="utf-8"))
        findings = compare(expectations, actual)
    except (ExpectationsError, OSError, json.JSONDecodeError) as exc:
        print(f"cannot measure: {exc}", file=sys.stderr)
        return 2

    if getattr(args, "expectations_hash", False):
        digest = hashlib.sha256(Path(args.expectations).read_bytes()).hexdigest()
        print(f"expectations sha256: {digest}")

    findings = merge_ledger(Path(args.ledger) if args.ledger else None, findings)

    if args.json:
        print(json.dumps(findings, indent=2, sort_keys=True))
    else:
        if not findings:
            print(f"{actual.get('host')}: every declared unit and listener is present")
        for finding in findings:
            print(
                f"[{finding['severity']}] {finding['state']} x{finding['count']} "
                f"{finding['subject']}\n    evidence: {finding['evidence']}"
            )

    if getattr(args, "fail_on", "high") == "none":
        return 0
    escalations = [
        f for f in findings if args.fail_on == "warning" or f["severity"] == "high"
    ]
    return 1 if escalations else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--expectations",
        default=str(Path(__file__).resolve().parents[1] / "rules/inside/expectations.yaml"),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="measure this host (run on the host)")
    p_collect.add_argument("--host", required=True)
    p_collect.add_argument("--out", required=True)
    p_collect.set_defaults(func=cmd_collect)

    p_check = sub.add_parser("check", help="diff a collection against the declarations")
    p_check.add_argument("--actual", required=True)
    p_check.add_argument("--ledger")
    p_check.add_argument("--json", action="store_true")
    p_check.add_argument(
        "--fail-on",
        choices=["high", "warning", "none"],
        default="high",
        help="which severity makes exit 1; `none` reports without failing",
    )
    p_check.add_argument(
        "--expectations-hash",
        action="store_true",
        help="print the declaration digest, so a changed contract is visible in the record",
    )
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
