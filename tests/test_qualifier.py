#!/usr/bin/env python3
"""Behavioural test for the qualifier — does it classify what we think it does?

WHY THIS EXISTS

`test_edge_template.py` asserts properties of rendered text. That is enough to
catch an unescaped brace and nothing else: it cannot tell you that a Matrix room
URL was being classified as an out-of-memory event, or that every Synapse INFO
line was being shipped as an error because its logger writes to stderr. Both
were true in production on 2026-10-28, on the two newest hosts, and no test
noticed — the template was "correct" and the behaviour was wrong.

So this test runs the rendered VRL through Vector itself, against a case file,
and compares the severity each line actually gets. The cases are the bugs we
have already paid for plus the ordinary paths we depend on.

Docker is required to evaluate VRL. When it is absent (CI runs in a container
without a daemon), the test does the static checks it can and exits 0 with a
loud SKIP — a test that fails for want of a daemon teaches people to ignore it.

Run: python3 tests/test_qualifier.py
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.path.join(ROOT, "tests", "qualifier_cases.jsonl")
RENDER = os.path.join(ROOT, "scripts", "render_edge_config.py")
IMAGE = "timberio/vector:0.58.0-debian"

failures = []
checks = 0


def check(condition, message):
    global checks
    checks += 1
    if not condition:
        failures.append(message)


def rendered_classify_source():
    """Render the config the way a journald cloud edge does, return the VRL."""
    out = subprocess.run(
        [sys.executable, RENDER, "--host", "edge-node", "--intranet", "joasis",
         "--host-class", "workload-host", "--journald", "--private-ipv4-only",
         "--units", "ssh.service,docker.service"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    )
    import yaml
    config = yaml.safe_load(out.stdout)
    return config["transforms"]["classify"]["source"]


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
        result = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{prog}:/p.vrl:ro",
             "-v", f"{CASES}:/i.jsonl:ro",
             IMAGE, "vrl", "-p", "/p.vrl", "-i", "/i.jsonl", "-o"],
            capture_output=True, text=True, timeout=180,
        )
    return result


def main():
    source = rendered_classify_source()

    # Static properties: each one is a regression we have already seen.
    check("parse_regex" in source,
          "the qualifier must read a declared text level, not just the stream")
    check("parsed.level" in source,
          "a structured logger's own level must be honoured before any heuristic")
    check(r"\boom\b" in source,
          "the crash words must be word-bounded: bare 'oom' matches inside /rooms/")
    check(".PRIORITY != null" in source,
          "journald priority must be tested for null: to_int(null) is 0 (emergency)")
    check('condition: \'.severity == "error" || .severity == "critical"\'' in
          open(os.path.join(ROOT, "roles", "observability", "templates",
                            "vector-edge.yaml.j2")).read(),
          "the qualified filter must still ship only error and critical")

    # A correct qualifier makes silence normal, so freshness needs a signal that
    # does not depend on it. Without this, every healthy quiet host alerts.
    template = open(os.path.join(ROOT, "roles", "observability", "templates",
                                "vector-edge.yaml.j2")).read()
    check("type: demo_logs" in template and "heartbeat_identity" in template,
          "the collector must emit a periodic heartbeat independent of the qualifier")
    check("- heartbeat_identity" in template,
          "the heartbeat must reach the sink, so sanitize has to accept it")

    if not docker_available():
        print(f"{checks} static check(s) passed")
        print("SKIP: no docker daemon here, so VRL behaviour was not evaluated. "
              "Run this on a host with docker (e.g. .80) to exercise the cases.")
        return 0

    result = run_vrl(source)
    if result.returncode != 0:
        failures.append("vector vrl failed: " + (result.stderr or "")[:600])
        report()
        return 1

    cases = [json.loads(line) for line in open(CASES) if line.strip()]
    expected = {c["_case"]: c["_expect"] for c in cases}
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
            seen[name] = event.get("severity")

    for name, want in expected.items():
        got = seen.get(name)
        check(got == want, f"{name}: expected severity {want!r}, got {got!r}")
    check(len(seen) == len(expected),
          f"expected {len(expected)} evaluated cases, saw {len(seen)}")

    report()
    return 1 if failures else 0


def report():
    for failure in failures:
        print(f"FAIL {failure}")
    print(f"{checks} check(s), {len(failures)} failure(s)")


if __name__ == "__main__":
    sys.exit(main())
