#!/usr/bin/env python3
"""Render the edge collector config for validation, without touching a host.

WHY THIS EXISTS
Vector compiles VRL at startup: a bad expression is not a test failure, it is a
container that restart-loops on a live node. Three such errors were found in
production on 2026-10-28 (fallible assignment, fallible predicate, unnecessary
coalescing) — every one would have been caught by `vector validate`.

USAGE
    python3 scripts/render_edge_config.py --http --journald > /tmp/vector.yaml
    docker run --rm -v /tmp/vector.yaml:/etc/vector/vector.yaml:ro \
        timberio/vector:0.58.0-debian validate --no-environment

Requires jinja2 and pyyaml (see requirements-dev.txt).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "roles" / "observability" / "templates"
DEFAULTS = ROOT / "roles" / "observability" / "defaults" / "edge-collector.yml"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="edge-node")
    parser.add_argument("--intranet", default="joasis")
    parser.add_argument("--host-class", default="workload-host")
    parser.add_argument("--http", action="store_true",
                        help="enable the public edge lens")
    parser.add_argument("--http-container", default="traefik",
                        help="container whose stdout carries the proxy access log "
                             "(traefik on the DMZ, mini-traefik on the LAN mini)")
    parser.add_argument("--lg", action="store_true",
                        help="enable the looking-glass decision lens")
    parser.add_argument("--lg-container", default="looking-glass")
    parser.add_argument("--portal", action="store_true",
                        help="enable the application portal's uvicorn access lens")
    parser.add_argument("--portal-container", default="activitypub-portal")
    parser.add_argument("--journald", action="store_true")
    parser.add_argument("--units", default="ssh.service,docker.service")
    parser.add_argument("--caddy", action="store_true",
                        help="enable the Caddy JSON access-log lens")
    parser.add_argument("--synapse", action="store_true",
                        help="enable the Synapse access-log lens")
    parser.add_argument("--private-ipv4-only", action="store_true")
    args = parser.parse_args()

    defaults = yaml.safe_load(DEFAULTS.read_text())
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)),
                      keep_trailing_newline=True)
    env.filters["bool"] = lambda v: str(v).strip().lower() in ("true", "1", "yes", "on")

    ctx = dict(defaults)
    ctx.update({
        "inventory_hostname": args.host,
        "vector_edge_host_name": args.host,
        "vector_edge_intranet": args.intranet,
        "vector_edge_host_class": args.host_class,
        "vector_edge_http_access": args.http,
        "vector_edge_http_container": args.http_container,
        "vector_edge_lg_access": args.lg,
        "vector_edge_lg_container": args.lg_container,
        "vector_edge_portal_access": args.portal,
        "vector_edge_portal_container": args.portal_container,
        "vector_edge_caddy_access": args.caddy,
        "vector_edge_synapse_access": args.synapse,
        "vector_sanitize_private_ipv4_only": args.private_ipv4_only,
        "vector_edge_journald": args.journald,
        "vector_edge_journald_units": [u for u in args.units.split(",") if u],
        "vector_collector_image": (
            defaults["vector_collector_image_journald"] if args.journald
            else defaults["vector_collector_image_default"]
        ),
    })
    sys.stdout.write(env.get_template("vector-edge.yaml.j2").render(**ctx))
    return 0


if __name__ == "__main__":
    sys.exit(main())
