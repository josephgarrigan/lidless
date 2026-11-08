#!/usr/bin/env python3
"""Contract and functional checks for host freshness checking.

Checks:
  - All hosts report -> ok
  - Silent hosts flagged -> silent
  - Unreachable centre handled without unhandled exception -> unreachable
  - Canonical fleet hosts default correctly
  - JSON output integrity
"""

import os
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from check_host_freshness import evaluate_freshness, CANONICAL_HOSTS

failures = []
checks = 0


def check(condition, message):
    global checks
    checks += 1
    if not condition:
        failures.append(message)


def test_all_hosts_healthy():
    def mock_query(centre, window):
        return [
            {"host": "control-plane", "n": "42"},
            {"host": "media-node", "n": "15"},
            {"host": "office-host", "n": "120"},
        ]

    res = evaluate_freshness(
        hosts=["control-plane", "media-node", "office-host"],
        query_fn=mock_query,
    )
    check(res["status"] == "ok", "all reporting hosts must return ok status")
    check(len(res["silent"]) == 0, "no hosts should be silent")
    check(res["counts"]["control-plane"] == 42, "event count must match")


def test_silent_host_detected():
    def mock_query(centre, window):
        return [
            {"host": "control-plane", "n": "42"},
        ]

    res = evaluate_freshness(
        hosts=["control-plane", "federation-node"],
        query_fn=mock_query,
    )
    check(res["status"] == "silent", "missing host must trigger silent status")
    check(res["silent"] == ["federation-node"], "silent host must be identified")
    check("federation-node" in res["expected"], "silent host is in expected list")


def test_centre_unreachable():
    def mock_query(centre, window):
        raise urllib.error.URLError("Connection refused")

    res = evaluate_freshness(
        centre="http://192.0.2.2:9428",
        hosts=["control-plane"],
        query_fn=mock_query,
    )
    check(res["status"] == "unreachable", "URLError must yield unreachable status")
    check("Connection refused" in res["error"], "error message must be preserved")
    check(res["silent"] == ["control-plane"], "unreachable centre marks hosts as not seen")


def test_default_canonical_hosts():
    def mock_query(centre, window):
        return [{"host": h, "n": "5"} for h in CANONICAL_HOSTS]

    res = evaluate_freshness(hosts=None, query_fn=mock_query)
    check(res["status"] == "ok", "canonical hosts must all report ok")
    check(res["expected"] == CANONICAL_HOSTS, "hosts must default to CANONICAL_HOSTS")
    check("vm-dmz-proxy" in res["expected"], "GCP DMZ is canonical")
    check("federation-node" in res["expected"], "OCI Synapse edge is canonical")


def test_stray_hosts_detected():
    def mock_query(centre, window):
        return [
            {"host": "control-plane", "n": "10"},
            {"host": "old-decommissioned-box", "n": "2"},
        ]

    res = evaluate_freshness(hosts=["control-plane"], query_fn=mock_query)
    check(res["status"] == "ok", "status is ok if all expected reported")
    check(res["stray"] == ["old-decommissioned-box"], "unexpected host is reported as stray")


def main():
    test_all_hosts_healthy()
    test_silent_host_detected()
    test_centre_unreachable()
    test_default_canonical_hosts()
    test_stray_hosts_detected()

    if failures:
        print(f"FAILED: {len(failures)} of {checks} checks")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"all {checks} freshness checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
