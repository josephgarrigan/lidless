#!/usr/bin/env python3
"""Every registered host must have reported within the window.

The failure this exists for: a quiet host and a broken collector look identical
from the centre. Zero rows is not health — it is absence of evidence. On
2026-10-27 media-node showed no events and only an injected probe distinguished
"nothing to say" from "the pipe is dead".

Usage:
    python3 scripts/check_host_freshness.py \
        --centre http://192.0.2.2:9428 \
        --hosts control-plane,media-node,office-host \
        --window 6h

Exit 0 = every host reported. Exit 1 = at least one silent, and it is named.
Exit 2 = the centre itself is unreachable (a different fault).

Stdlib only, so it can run from a cron job, a Windmill job, or CI.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

QUERY = '* | stats by (host) count() as n'

CANONICAL_HOSTS = [
    "control-plane",
    "media-node",
    "media-node",
    "office-host",
    "vm-dmz-proxy",
    "federation-node",
]


def query_centre(centre: str, window: str, limit: int = 1000) -> list[dict]:
    body = urllib.parse.urlencode(
        {"query": QUERY, "start": window, "limit": str(limit)}
    ).encode()
    request = urllib.request.Request(
        f"{centre.rstrip('/')}/select/logsql/query",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode("utf-8", "replace")
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def evaluate_freshness(
    centre: str = "http://192.0.2.2:9428",
    hosts: list[str] | None = None,
    window: str = "6h",
    query_fn=query_centre,
) -> dict:
    if hosts is None or len(hosts) == 0:
        expected = list(CANONICAL_HOSTS)
    else:
        expected = [h.strip() for h in hosts if h.strip()]

    try:
        rows = query_fn(centre, window)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "status": "unreachable",
            "centre": centre,
            "window": window,
            "error": str(exc),
            "expected": expected,
            "silent": expected,
            "stray": [],
            "counts": {},
        }

    seen = {row.get("host"): int(row.get("n") or 0) for row in rows if row.get("host")}
    silent = [h for h in expected if not seen.get(h)]
    stray = sorted(k for k in set(seen) - set(expected) if k)

    return {
        "status": "ok" if not silent else "silent",
        "centre": centre,
        "window": window,
        "expected": expected,
        "silent": silent,
        "stray": stray,
        "counts": seen,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--centre", default="http://192.0.2.2:9428")
    parser.add_argument(
        "--hosts",
        default="",
        help="comma-separated canonical host names (defaults to canonical fleet hosts)",
    )
    parser.add_argument("--window", default="6h")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit structured JSON result for automation/Windmill",
    )
    args = parser.parse_args()

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()] if args.hosts else None
    result = evaluate_freshness(args.centre, hosts=hosts, window=args.window)

    if args.json:
        print(json.dumps(result, indent=2))
        if result["status"] == "unreachable":
            return 2
        return 1 if result["silent"] else 0

    if result["status"] == "unreachable":
        print(f"CENTRE UNREACHABLE: {result['centre']} ({result.get('error')})")
        return 2

    expected = result["expected"]
    seen = result["counts"]
    silent = result["silent"]
    stray = result["stray"]

    for host in expected:
        state = "ok  " if seen.get(host) else "SILENT"
        print(f"  {state} {host:<20} {seen.get(host, 0)} event(s) in {args.window}")

    if stray:
        print(f"  note  reporting but not registered: {', '.join(stray)}")

    if silent:
        print(f"FAILED: {len(silent)} of {len(expected)} host(s) silent: {', '.join(silent)}")
        return 1
    print(f"all {len(expected)} host(s) reported within {args.window}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
