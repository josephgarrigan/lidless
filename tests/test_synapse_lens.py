#!/usr/bin/env python3
"""Behavioural test for the Synapse access lens.

WHY THIS EXISTS

The Synapse lens decides what leaves the host by *suppressing* traffic: 88.8%
of calls are /sync, /typing and /versions. A regex that silently stops matching
does not error — it aborts every line, and the host goes quiet in exactly the
way a healthy host looks. So the suppression decision and the fields it depends
on are checked against real-shaped log lines, not against template text.

It also checks the two defects this lens exists to surface:

  * a rejected E2E key upload is a 400, so no error line exists anywhere; the
    lens must classify it as `keys_upload` and mark it dangerous
  * an appservice logs "@app|@user"; treated as one string, the actor is lost

Docker is required to evaluate VRL. When it is absent (CI has no daemon), the
test runs its static checks and exits 0 with a loud SKIP.

Run: python3 tests/test_synapse_lens.py
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.path.join(ROOT, "tests", "synapse_cases.jsonl")
RENDER = os.path.join(ROOT, "scripts", "render_edge_config.py")
IMAGE = "timberio/vector:0.58.0-debian"

failures = []
checks = 0


def check(condition, message):
    global checks
    checks += 1
    if not condition:
        failures.append(message)


def rendered():
    out = subprocess.run(
        [sys.executable, RENDER, "--host", "federation-node", "--intranet",
         "oci-daherd01", "--host-class", "edge", "--journald", "--synapse",
         "--private-ipv4-only", "--units", "ssh.service,docker.service"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    )
    import yaml
    config = yaml.safe_load(out.stdout)
    return config


def docker_available():
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_vrl(program):
    with tempfile.TemporaryDirectory() as tmp:
        prog = os.path.join(tmp, "program.vrl")
        with open(prog, "w") as fh:
            fh.write(program + "\n.\n")
        return subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{prog}:/p.vrl:ro",
             "-v", f"{CASES}:/i.jsonl:ro",
             IMAGE, "vrl", "-p", "/p.vrl", "-i", "/i.jsonl", "-o"],
            capture_output=True, text=True, timeout=180,
        )


def main():
    config = rendered()
    source = config["transforms"]["synapse_access"]["source"]

    # Static properties the fixture cannot see.
    check("Processed request:" in source,
          "the lens must select Synapse access records and nothing else")
    check("suppressed" in source and "drop_reason" in source,
          "suppression must be explicit and carry a reason that becomes a count")
    check("keys_upload" in source,
          "a rejected E2E key upload needs its own class")
    check("split(ur, \"|\")" in source,
          "an appservice's '@app|@user' identity must be split")
    check("bridge_protocol" in source,
          "the lens must classify bridge protocols for appservice traffic")
    check("bridge_error" in source,
          "bridge errors must be elevated to bridge_error class")
    check("synapse_ship" in config["transforms"]["sanitize"]["inputs"],
          "shipped events must reach the sink through sanitize")
    check("synapse_drop_finalize" in config["transforms"]["sanitize"]["inputs"],
          "suppressed traffic must be counted, not silently lost")
    check(config["transforms"]["synapse_drop_rollup"]["flush_period_ms"] == 3600000,
          "suppression counters must flush hourly")
    check("synapse.access.http" in config["transforms"]["classify"]["source"],
          "the generic qualifier must abort access records or 5xx ships twice")

    if not docker_available():
        print(f"{checks} static check(s) passed")
        print("SKIP: no docker daemon here, so VRL behaviour was not evaluated.")
        return 0

    result = run_vrl(source)
    if result.returncode != 0:
        failures.append("vector vrl failed: " + (result.stderr or "")[:600])
        return report()

    cases = [json.loads(line) for line in open(CASES) if line.strip()]
    expected = {c["_case"]: c for c in cases}
    seen = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = event.get("_case")
        if name in expected:
            seen[name] = event

    shipped = sum(1 for c in cases if c.get("_expect") != "abort")
    for name, case in expected.items():
        if case.get("_expect") == "abort":
            check(name not in seen, f"{name}: expected the lens to abort it")
            continue
        event = seen.get(name)
        if event is None:
            check(False, f"{name}: expected a parsed event, saw none")
            continue
        check(event.get("suppressed") == case["_expect_suppressed"],
              f"{name}: expected suppressed {case['_expect_suppressed']!r}, "
              f"got {event.get('suppressed')!r}")
        if "_expect_drop" in case:
            check(event.get("drop_reason") == case["_expect_drop"],
                  f"{name}: expected drop_reason {case['_expect_drop']!r}, "
                  f"got {event.get('drop_reason')!r}")
        if "_expect_class" in case:
            check(event.get("lens_class") == case["_expect_class"],
                  f"{name}: expected lens_class {case['_expect_class']!r}, "
                  f"got {event.get('lens_class')!r}")
        if "_expect_danger" in case:
            check(event.get("danger") == case["_expect_danger"],
                  f"{name}: expected danger {case['_expect_danger']!r}, "
                  f"got {event.get('danger')!r}")
        if "_expect_user" in case:
            check(event.get("user_id") == case["_expect_user"],
                  f"{name}: expected user_id {case['_expect_user']!r}, "
                  f"got {event.get('user_id')!r}")
        if "_expect_acting" in case:
            check(event.get("acting_as") == case["_expect_acting"],
                  f"{name}: expected acting_as {case['_expect_acting']!r}, "
                  f"got {event.get('acting_as')!r}")
        if "_expect_bridge" in case:
            check(event.get("bridge_protocol") == case["_expect_bridge"],
                  f"{name}: expected bridge_protocol {case['_expect_bridge']!r}, "
                  f"got {event.get('bridge_protocol')!r}")

    check(len(seen) == shipped,
          f"expected {shipped} parsed records, saw {len(seen)}")
    return report()


def report():
    if failures:
        print(f"FAILED: {len(failures)} of {checks} checks")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"OK: {checks} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
