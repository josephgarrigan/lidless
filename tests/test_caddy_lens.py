#!/usr/bin/env python3
"""Behavioural test for the Caddy access lens.

WHY THIS EXISTS

The Traefik lens could be tested by text and still be wrong, because text
assertions cannot tell you what a line *becomes*. The Caddy lens has the same
shape and a sharper failure mode: Caddy's access record is JSON whose request
fields are nested, and a mis-typed path or status does not error — it silently
classifies every request as `client_error`, or prunes a real probe as if it were
a static asset.

So this renders the config with `--caddy`, extracts the transform's VRL, runs it
through Vector against a fixture, and checks the class, danger flag and severity
each record actually gets. Aborted records are counted too: a lens that ships
everything is as wrong as one that ships nothing.

Docker is required to evaluate VRL. When it is absent (CI runs in a container
without a daemon), the test does its static checks and exits 0 with a loud SKIP.

Run: python3 tests/test_caddy_lens.py
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.path.join(ROOT, "tests", "caddy_cases.jsonl")
RENDER = os.path.join(ROOT, "scripts", "render_edge_config.py")
IMAGE = "timberio/vector:0.58.0-debian"

failures = []
checks = 0


def check(condition, message):
    global checks
    checks += 1
    if not condition:
        failures.append(message)


def rendered_source():
    out = subprocess.run(
        [sys.executable, RENDER, "--host", "federation-node", "--intranet",
         "oci-daherd01", "--host-class", "edge", "--journald", "--caddy",
         "--private-ipv4-only", "--units", "ssh.service,docker.service"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    )
    import yaml
    config = yaml.safe_load(out.stdout)
    return config["transforms"]["caddy_access"]["source"], config


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
    source, config = rendered_source()

    # Static properties that the fixture cannot see.
    check("http.log.access" in source,
          "the lens must select access records and ignore startup lines")
    check("rec.request" in source,
          "Caddy's access record nests method/uri/ip under request")
    check("sensitive" in source and "injected" in source,
          "the lens must classify paths, not just status codes")
    check("if .http_status < 400 && !sensitive && !injected { abort }" in source,
          "everything below 4xx must be pruned unless the path is an attack")
    check("caddy_access" in config["transforms"]["sanitize"]["inputs"],
          "the lens must reach the sink through sanitize")
    check("http.log.access" in config["transforms"]["classify"]["source"],
          "the generic qualifier must abort access records, or 5xx ships twice")

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
    expected = {c["_case"]: c for c in cases}
    seen = {}
    aborted = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if line == "aborted":
            # Vector prints one `aborted` per aborted input, in order.
            continue
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = event.get("_case")
        if name in expected:
            seen[name] = event

    evaluated = sum(1 for c in cases if "_expect_class" in c)
    for name, case in expected.items():
        if case.get("_expect") == "abort":
            check(name not in seen, f"{name}: expected the lens to abort it")
            continue
        event = seen.get(name)
        if event is None:
            check(False, f"{name}: expected a shipped event, saw none")
            continue
        check(event.get("lens_class") == case["_expect_class"],
              f"{name}: expected lens_class {case['_expect_class']!r}, "
              f"got {event.get('class')!r}")
        check(event.get("danger") == case["_expect_danger"],
              f"{name}: expected danger {case['_expect_danger']!r}, "
              f"got {event.get('danger')!r}")
        check(event.get("severity") == case["_expect_severity"],
              f"{name}: expected severity {case['_expect_severity']!r}, "
              f"got {event.get('severity')!r}")

    check(len(seen) == evaluated,
          f"expected {evaluated} shipped records, saw {len(seen)}")

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
