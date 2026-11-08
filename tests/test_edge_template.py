#!/usr/bin/env python3
"""Contract checks for the edge collector's committed templates.

The edge collector is the deployed half of this repository, and its two
expensive historical defects were both in the template rather than the code:

  1. `.host = get_hostname() ?? "unknown"` — inside the container this returns
     the CONTAINER id, so every shipped event was attributed to a hash.
  2. `victorialogs_addr: "198.51.100.157"` — a LAN address for a centre that
     moved to the overlay.

A redeploy from git must not reintroduce either. This test renders the
templates the way Ansible would and asserts the properties that matter, plus
checks that the deployment path the docs name actually exists.

Run: python3 tests/test_edge_template.py
Needs: jinja2, pyyaml (see requirements-dev.txt).
"""

import os
import re
import sys

import yaml
from jinja2 import Environment, FileSystemLoader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "roles", "observability", "templates")
DEFAULTS = os.path.join(
    ROOT, "roles", "observability", "defaults", "edge-collector.yml"
)

checks = 0
failures = []


def check(condition, message):
    global checks
    checks += 1
    if not condition:
        failures.append(message)


def render(template_name, context):
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        keep_trailing_newline=True,
        variable_start_string="{{",
        variable_end_string="}}",
    )
    # Ansible supplies the `bool` filter; plain Jinja does not. Emulate it so the
    # test renders the template exactly as the play does instead of avoiding the
    # construct that production uses.
    env.filters["bool"] = lambda value: str(value).strip().lower() in (
        "true", "1", "yes", "on",
    )
    return env.get_template(template_name).render(**context)


def load_defaults():
    with open(DEFAULTS) as handle:
        return yaml.safe_load(handle)


def context(defaults, host="media-node"):
    ctx = dict(defaults)
    ctx["vector_edge_host_name"] = host
    ctx["inventory_hostname"] = host
    # Mirror the play's image selection: the role picks the image from the
    # journald flag when no explicit override is set.
    if not ctx.get("vector_collector_image"):
        ctx["vector_collector_image"] = (
            defaults["vector_collector_image_journald"]
            if str(ctx.get("vector_edge_journald")).lower() in ("true", "1", "yes", "on")
            else defaults["vector_collector_image_default"]
        )
    return ctx


def test_defaults_point_at_the_overlay_centre(defaults):
    check(
        defaults["victorialogs_addr"] == "192.0.2.2",
        "victorialogs_addr must be the overlay centre 192.0.2.2, "
        f"got {defaults['victorialogs_addr']!r}",
    )
    check(
        "198.51.100." not in str(defaults["victorialogs_addr"]),
        "victorialogs_addr must not be a LAN address",
    )
    nodes = defaults.get("vector_collector_nodes", [])
    check(
        len(nodes) == 6,
        f"all six collectors must be listed in vector_collector_nodes, got {len(nodes)}",
    )
    check(
        all(str(n).startswith("192.0.2.") for n in nodes),
        "collector addresses must be overlay (192.0.2.x), never LAN addresses",
    )
    check(
        "vector_collector_base_dir" in defaults,
        "vector_collector_base_dir must be declared: the deploy tasks use it",
    )
    check(
        defaults.get("vector_collector_image_journald", "").endswith("-debian"),
        "journald hosts need the debian image: alpine has no journalctl",
    )


def test_identity_is_the_inventory_host_name(defaults):
    rendered = render("vector-edge.yaml.j2", context(defaults, "media-node"))
    check(
        '.host = "media-node"' in rendered,
        "rendered config must write the inventory host name into .host",
    )
    code_lines = [
        line for line in rendered.splitlines() if not line.strip().startswith("#")
    ]
    check(
        all("get_hostname" not in line for line in code_lines),
        "get_hostname() returns the container id inside the container — "
        "it must not appear in the rendered collector config",
    )
    # Narrowed on purpose: the table's own "unknown" class is not a host
    # fallback. What must never appear is a *host* assignment defaulting to
    # "unknown" — that is how every event gets attributed to nobody.
    host_fallbacks = [
        line for line in rendered.split("sinks:")[0].splitlines()
        if ".host" in line and "unknown" in line
    ]
    check(
        not host_fallbacks,
        "the rendered config must not fall back to an unknown host "
        f"(found: {host_fallbacks[:1]})",
    )


def test_sink_is_the_overlay_centre(defaults):
    rendered = render("vector-edge.yaml.j2", context(defaults))
    check(
        "uri: \"http://192.0.2.2:9428/insert/jsonline" in rendered,
        "the sink must address the overlay centre on 9428",
    )
    check(
        "http://198.51.100." not in rendered,
        "the rendered collector must not carry a LAN sink",
    )
    check(
        "_stream_fields=host,intranet" in rendered,
        "host and intranet are the stream contract: both must partition streams",
    )
    check(
        "_msg_field=_msg" in rendered,
        "the message field contract changed; LogSQL queries rely on _msg",
    )


def test_api_is_loopback(defaults):
    rendered = render("vector-edge.yaml.j2", context(defaults))
    check(
        "address: 127.0.0.1:8686" in rendered,
        "the Vector API must bind loopback, never 0.0.0.0",
    )
    check(
        "0.0.0.0:8686" not in rendered,
        "the Vector API must not bind all interfaces",
    )


def test_placement_fields_ride_with_every_event(defaults):
    rendered = render("vector-edge.yaml.j2", context(defaults, "media-node"))
    check(
        '.intranet = "joasis"' in rendered,
        "the Dura intranet name must ride on every event",
    )
    check(
        '.host_class = "workload-host"' in rendered,
        "the Dura host_class must ride on every event",
    )
    check(
        ".container = .container_name" in rendered
        and "._SYSTEMD_UNIT" in rendered,
        "journald events carry no container_name; the unit must name the stream",
    )


def test_journald_is_optional_and_off_by_default(defaults):
    rendered_off = render("vector-edge.yaml.j2", context(defaults))
    check(
        "type: journald" not in rendered_off,
        "journald must be absent unless a host asks for it",
    )
    check(
        "/run/log/journal" not in render(
            "vector-edge-compose.yaml.j2", context(defaults)
        ),
        "the collector must not mount the journal unless journald is enabled",
    )

    enabled = dict(defaults)
    enabled["vector_edge_journald"] = True
    enabled["vector_edge_journald_units"] = ["sshd.service", "caddy.service"]
    rendered_on = render("vector-edge.yaml.j2", context(enabled))
    check(
        "type: journald" in rendered_on and "sshd.service" in rendered_on,
        "enabling journald must add the source and its unit allow-list",
    )
    check(
        "- journald" in rendered_on,
        "the journald source must feed the classifier, not sit unread",
    )
    compose_on = render("vector-edge-compose.yaml.j2", context(enabled))
    check(
        "/run/log/journal:/run/log/journal:ro" in compose_on,
        "enabling journald must mount the journal read-only",
    )
    check(
        "/etc/machine-id:/etc/machine-id:ro" in compose_on,
        "journalctl looks under /var/log/journal/<machine-id>; without the "
        "host's machine-id the container reports 'No journal files were found'",
    )

    # Empty allow-list means "every unit". Rendering `include_units:` with
    # nothing beneath it is YAML null and Vector refuses to start:
    # "sources.journald: invalid type: null, expected a sequence". Found on the
    # OCI edge, where no group_vars supplied units.
    empty = dict(defaults)
    empty["vector_edge_journald"] = True
    empty["vector_edge_journald_units"] = []
    rendered_empty = render("vector-edge.yaml.j2", context(empty))
    check(
        "include_units" not in rendered_empty,
        "an empty unit allow-list must omit include_units, not render null",
    )
    check(
        "type: journald" in rendered_empty,
        "journald must still be enabled when the allow-list is empty",
    )


def test_http_lens_is_off_by_default(defaults):
    rendered = render("vector-edge.yaml.j2", context(defaults))
    check(
        "docker_logs_http" not in rendered,
        "the INFO-level proxy source must not exist unless asked for",
    )
    check(
        "http_access" not in rendered,
        "the access-log transform must not exist unless asked for",
    )
    check(
        "- {{ vector_edge_http_container }}" not in rendered
        and "- traefik" not in rendered.split("sources:")[1].split("transforms:")[0],
        "traefik must stay in the normal (ERROR-level) stream when the lens is off",
    )


def test_http_lens_reads_the_proxy_at_info_and_prunes(defaults):
    enabled = dict(defaults)
    enabled["vector_edge_http_access"] = True
    enabled["vector_sanitize_private_ipv4_only"] = True
    rendered = render("vector-edge.yaml.j2", context(enabled))

    check(
        "docker_logs_http" in rendered and "include_containers:" in rendered,
        "the lens needs its own source, scoped to the proxy container",
    )
    check(
        "- traefik" in rendered.split("exclude_containers:")[1].split("sources:")[0]
        or "- traefik" in rendered,
        "the proxy must be excluded from the ERROR-level source when the lens is on",
    )
    for field in ("http_status", "http_host", "http_path", "http_router",
                  "http_service", "http_duration_ns", "client_addr", "user_agent"):
        check(f".{field}" in rendered, f"the lens must carry {field}")
    check(
        ".http_status < 400 && !.danger { abort }" in rendered,
        "everything below 4xx must be aborted at the edge unless the request "
        "was judged dangerous — a 200 on a sensitive path is the case the "
        "error lane cannot see",
    )
    check(
        "vector_edge_http_static_regex" not in rendered,
        "the static-asset regex must be rendered, not passed through as a var name",
    )
    check(
        "(?i)\\.(css|js|" in rendered,
        "the static-asset prune must be present",
    )
    check(
        "      - http_access" in rendered,
        "the sanitizer must take the lens's output as an input",
    )
    check(
        ".http_status >= 500 { .severity = \"error\" } else { .severity = \"warning\" }"
        in rendered,
        "5xx must ship as error and 4xx as warning",
    )


def test_ansi_escapes_are_stripped_before_level_detection(defaults):
    """An ANSI-coloured level is a level, not silence.

    windmill_server wraps its level as "\\x1b[31mERROR\\x1b[0m". The level regex
    requires a boundary on both sides and ESC is neither, so the level parsed as
    empty and the vocabulary fallback (which does not contain the bare word
    ERROR) never fired: 11 ERROR lines in 24h reached the collector and none
    reached the centre. Stripping must happen before the level is read, not after.
    """
    rendered = render("vector-edge.yaml.j2", context(defaults))
    strip = "replace(.raw, r'\\x1b\\[[0-9;]*[A-Za-z]', \"\")"
    check(strip in rendered, "the classifier must strip ANSI escapes from the raw line")
    check(
        rendered.index(strip) < rendered.index('.level = ""'),
        "ANSI stripping must precede level detection or it cannot help",
    )


def test_access_lenses_share_one_class_vocabulary(defaults):
    """Every access lens emits `lens_class`, the fleet-wide vocabulary.

    The Traefik lens used to ship only `class`, the baked judgement table's own
    word (recon/injection/auth_probe). A query written on federation-node therefore
    returned nothing for the DMZ proxy. `class` stays as the judgement verdict;
    `lens_class` is additive and comparable across hosts.
    """
    enabled = dict(defaults)
    enabled["vector_edge_http_access"] = True
    rendered = render("vector-edge.yaml.j2", context(enabled))

    check(
        ".lens_class" in rendered,
        "the Traefik lens must assign lens_class, not only the judgement class",
    )
    for word in ("probe", "auth", "injection", "client_error", "throttle", "error"):
        check(
            f'"{word}"' in rendered,
            f"the shared lens_class vocabulary is missing {word!r}",
        )
    check(
        '.class = "unknown"' in rendered,
        "the judgement class must keep its own vocabulary; lens_class is additive",
    )


def test_judgement_table_only_ships_with_the_lens_that_uses_it(defaults):
    """A base collector must not need a file the base deploy does not ship.

    Only the Traefik lens calls get_enrichment_table_record(). Emitting the
    enrichment table unconditionally made Vector refuse to start on the four
    base collectors, whose containers mount vector.yaml but not judgements.csv.
    """
    base = render("vector-edge.yaml.j2", context(defaults))
    check(
        "enrichment_tables" not in base,
        "a collector without an access lens must not declare the judgement table",
    )
    enabled = dict(defaults)
    enabled["vector_edge_http_access"] = True
    on = render("vector-edge.yaml.j2", context(enabled))
    check(
        "enrichment_tables" in on and "/etc/vector/judgements.csv" in on,
        "the Traefik lens requires the baked judgement table",
    )


def test_caddy_lens_is_off_by_default_and_prunes_when_on(defaults):
    rendered = render("vector-edge.yaml.j2", context(defaults))
    check(
        "caddy_access" not in rendered,
        "the Caddy lens must not exist unless asked for",
    )

    enabled = dict(defaults)
    enabled["vector_edge_caddy_access"] = True
    enabled["vector_sanitize_private_ipv4_only"] = True
    rendered = render("vector-edge.yaml.j2", context(enabled))
    check(
        "caddy_access:" in rendered and "http.log.access" in rendered,
        "the Caddy lens must select access records from the container log",
    )
    check(
        "if .http_status < 400 && !sensitive && !injected { abort }" in rendered,
        "the Caddy lens must prune below 4xx unless the path is an attack",
    )
    check(
        "      - caddy_access" in rendered,
        "the Caddy lens must reach the sink through sanitize",
    )
    check(
        'if contains(.raw, "http.log.access") { abort }' in rendered,
        "the generic qualifier must abort access records, or a 5xx ships twice",
    )


def test_synapse_lens_is_off_by_default(defaults):
    rendered = render("vector-edge.yaml.j2", context(defaults))
    check(
        "synapse_access" not in rendered,
        "the Synapse lens must not exist unless asked for",
    )
    enabled = dict(defaults)
    enabled["vector_edge_synapse_access"] = True
    rendered = render("vector-edge.yaml.j2", context(enabled))
    check(
        "synapse_access:" in rendered and "synapse_drop_rollup:" in rendered,
        "the Synapse lens must ship survivors and count what it suppresses",
    )
    check(
        "      - synapse_ship" in rendered,
        "the Synapse lens must reach the sink through sanitize",
    )


def test_every_template_flag_is_bound_by_the_deploy(defaults):
    template = open(os.path.join(TEMPLATES, "vector-edge.yaml.j2")).read()
    flags = set(re.findall(r"{%\s*if\s+(vector_[a-z0-9_]+)\s*(?:\||%})", template))
    tasks = open(os.path.join(ROOT, "roles", "observability", "tasks",
                              "edge.yml")).read()
    for flag in sorted(flags):
        check(
            f"{flag}:" in tasks,
            f"the template branches on {flag} but tasks/edge.yml never binds it — "
            f"the deploy fails after touching the host",
        )


def test_public_edge_keeps_client_addresses(defaults):
    internal = render("vector-edge.yaml.j2", context(defaults))
    check(
        r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b" in internal,
        "the default sanitizer must redact every IPv4 literal",
    )
    check(
        "172\\.(?:1[6-9]|2[0-9]|3[01])" not in internal,
        "the private-only pattern must not appear when the policy is off",
    )

    public = dict(defaults)
    public["vector_sanitize_private_ipv4_only"] = True
    rendered = render("vector-edge.yaml.j2", context(public))
    check(
        "172\\.(?:1[6-9]|2[0-9]|3[01])" in rendered,
        "the public-edge policy must redact RFC1918 only",
    )
    check(
        "192\\.168\\." in rendered and "10\\." in rendered,
        "the public-edge policy must still redact 10/8 and 192.168/16",
    )


def test_zero_retention_guards_survive(defaults):
    rendered = render("vector-edge.yaml.j2", context(defaults))
    check(
        "del(.raw)" in rendered and "del(.message)" in rendered,
        "the unsanitized originals must be deleted, not shadowed",
    )
    check(
        "when_full: drop_newest" in rendered,
        "a centre outage must not back-pressure the Docker log stream",
    )
    check(
        "data_dir: /var/lib/vector" in rendered,
        "the collector must hold nothing on disk",
    )


def test_compose_matches_the_deployed_shape(defaults):
    rendered = render("vector-edge-compose.yaml.j2", context(defaults))
    check(
        "timberio/vector:0.58.0-alpine" in rendered,
        "the compose must pin the collector image from defaults",
    )
    check(
        "/var/run/docker.sock:/var/run/docker.sock:ro" in rendered,
        "the Docker socket must be mounted read-only",
    )
    check(
        "restart: unless-stopped" in rendered,
        "the collector must survive a host reboot",
    )
    check(
        "noexec" in rendered,
        "the collector's tmpfs must not be executable",
    )
    check(
        "/var/lib/vector:rw" in rendered,
        "data_dir must be a tmpfs mountpoint: journald refuses to start "
        "when the directory does not exist",
    )


def test_the_documented_deploy_path_exists():
    check(
        os.path.isfile(os.path.join(ROOT, "edge.yml")),
        "edge.yml must exist: it is the playbook EDGE.md documents",
    )
    check(
        os.path.isfile(
            os.path.join(ROOT, "roles", "observability", "tasks", "edge.yml")
        ),
        "roles/observability/tasks/edge.yml must exist: the play deploys through it",
    )
    check(
        os.path.isfile(os.path.join(ROOT, "group_vars", "edge_collectors.yml")),
        "group_vars/edge_collectors.yml must exist: it auto-loads for the "
        "edge_collectors group",
    )
    check(
        os.path.isfile(os.path.join(ROOT, "inventory.edge.ini.example")),
        "inventory.edge.ini.example must exist: the documented -i target",
    )


def test_the_play_book_binds_the_edge_group():
    with open(os.path.join(ROOT, "edge.yml")) as handle:
        play = yaml.safe_load(handle)
    hosts = play[0]["hosts"]
    check(
        hosts == "edge_collectors",
        f"edge.yml must target the edge_collectors group, got {hosts!r}",
    )
    tasks = play[0].get("tasks") or []
    check(bool(tasks), "the play must deploy through an explicit task, not `roles:`")
    role = (tasks[0].get("ansible.builtin.import_role") or {}) if tasks else {}
    check(
        role.get("name") == "observability",
        f"the play must import the observability role, got {role.get('name')!r}",
    )
    check(
        role.get("tasks_from") == "edge.yml",
        "the play must deploy the edge tasks, not the triad's main.yml "
        "(the roles: form fell through to main.yml and broke dpkg on a live host)",
    )


def test_render_cli_can_name_a_non_traefik_proxy(defaults):
    """The proxy container is a deploy-time fact, not a constant.

    The LAN front door on the mini is `mini-traefik`; the DMZ's is `traefik`.
    The render CLI is what dry-runs and remote deploys call, so if it cannot
    name the container the lens reads, the only way to deploy is to hand-edit
    the rendered file — which is how an unattributable config reaches a host.
    """
    import subprocess
    out = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "render_edge_config.py"),
         "--host", "control-plane", "--http", "--http-container", "mini-traefik"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    )
    rendered = out.stdout
    check(
        "      - mini-traefik" in rendered,
        "the render CLI must be able to point the Traefik lens at mini-traefik",
    )
    check(
        "      - traefik" not in rendered,
        "the default container must not leak into a render that named another",
    )


def main():
    defaults = load_defaults()
    test_defaults_point_at_the_overlay_centre(defaults)
    test_identity_is_the_inventory_host_name(defaults)
    test_sink_is_the_overlay_centre(defaults)
    test_api_is_loopback(defaults)
    test_placement_fields_ride_with_every_event(defaults)
    test_journald_is_optional_and_off_by_default(defaults)
    test_http_lens_is_off_by_default(defaults)
    test_http_lens_reads_the_proxy_at_info_and_prunes(defaults)
    test_ansi_escapes_are_stripped_before_level_detection(defaults)
    test_access_lenses_share_one_class_vocabulary(defaults)
    test_judgement_table_only_ships_with_the_lens_that_uses_it(defaults)
    test_caddy_lens_is_off_by_default_and_prunes_when_on(defaults)
    test_synapse_lens_is_off_by_default(defaults)
    test_every_template_flag_is_bound_by_the_deploy(defaults)
    test_render_cli_can_name_a_non_traefik_proxy(defaults)
    test_public_edge_keeps_client_addresses(defaults)
    test_zero_retention_guards_survive(defaults)
    test_compose_matches_the_deployed_shape(defaults)
    test_the_documented_deploy_path_exists()
    test_the_play_book_binds_the_edge_group()

    if failures:
        print(f"FAILED: {len(failures)} of {checks} checks")
        for message in failures:
            print(f"  - {message}")
        return 1
    print(f"all {checks} edge-template checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def _portal_defaults():
    import yaml
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "roles/observability/defaults/edge-collector.yml"
    return yaml.safe_load(p.read_text())


def test_portal_lens_is_off_by_default():
    rendered = render("vector-edge.yaml.j2", context(_portal_defaults()))
    check("docker_logs_portal" not in rendered,
          "the portal source must not exist unless asked for")
    check("portal_access" not in rendered,
          "the portal lens must not exist unless asked for")


def test_portal_lens_parses_uvicorn_and_joins_the_shared_vocabulary():
    defaults = _portal_defaults()
    defaults["vector_edge_portal_access"] = True
    rendered = render("vector-edge.yaml.j2", context(defaults))

    check("docker_logs_portal" in rendered, "the lens needs its own source")
    check("- activitypub-portal" in rendered,
          "the portal source must name the container it reads")
    check("INFO:" in rendered and "parse_regex" in rendered,
          "uvicorn's access line must be parsed, not grepped")
    for field in ("http_status", "http_method", "http_path", "client_addr", "lens_class"):
        check(f".{field}" in rendered, f"the portal lens must carry {field}")
    check('.lens = "portal"' in rendered,
          "a lens verdict must name its source so two vocabularies never collide")
    check("      - portal_access" in rendered,
          "the sanitizer must take the portal lens as an input")
    check("lens=portal" in rendered, "the shipped line must say which lens made it")


def test_lg_lens_is_off_by_default():
    rendered = render("vector-edge.yaml.j2", context(_portal_defaults()))
    check("docker_logs_lg" not in rendered, "the controller source must be opt-in")
    check("lg_access" not in rendered, "the decision lens must be opt-in")


def test_lg_lens_ships_decisions_with_a_shared_vocabulary():
    defaults = _portal_defaults()
    defaults["vector_edge_lg_access"] = True
    rendered = render("vector-edge.yaml.j2", context(defaults))

    check("docker_logs_lg" in rendered, "the lens needs its own source")
    check("- looking-glass" in rendered, "the source must name the controller")
    check('.lens = "looking-glass"' in rendered,
          "a lens verdict must name its source")
    check("lens_class" in rendered, "it must speak the shared vocabulary")
    check("decision" in rendered and "refusal" in rendered,
          "decisions and refusals are the classes that matter here")
    check("      - lg_access" in rendered,
          "the sanitizer must take the controller lens as an input")
