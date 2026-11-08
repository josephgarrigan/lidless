# Edge collector — the observer's peripheral vision

This is the **deployed** half of lidless. It is distinct from the
Vector + Dozzle + Gatus triad in `README.md` and the rest of
`roles/observability/`, which targets end-user container-log browsing and has
never been deployed anywhere. If you are reading this wondering why the repo
describes a 100 MB triad that no host runs, that is why.

## What it does

One Vector container per node. It reads the Docker log stream, **qualifies at
the edge**, sanitizes, and ships survivors to a central log store.

```
docker.sock:ro ─▶ classify ─▶ filter ─▶ sanitize ─▶ http ─▶ VictoriaLogs
                   │          │          │                     (centre)
              severity    only error/  redact, then
              scoring     critical     delete the originals
```

Deliberately **not** this play: Dozzle, Gatus, Traefik ingress, Matrix alerting.
Dozzle and Gatus run on the fleet as separate stacks (`/opt/obs`) and are not
installed by this role.

## The fleet as deployed (measured 2026-10-28)

- **Six collectors**, overlay-only: `control-plane` `192.0.2.8`, `media-node`
  `192.0.2.5`, `media-node` `192.0.2.3`, `office-host` `192.0.2.2`, and —
  since 2026-10-28 — `vm-dmz-proxy` `192.0.2.1` (GCP) and
  `federation-node` `192.0.2.7` (OCI, `150.136.83.245`) write the canonical names
  for the two cloud edges.
- **Cloud edges:** the GCP DMZ runs the public edge lens (Traefik). OCI
  `federation-node` runs the **Caddy access lens** (`vector_edge_caddy_access`):
  since 2026-10-29 its Caddyfile carries a global `log` block writing JSON to
  stdout, and the lens parses `http.log.access` records, prunes everything
  below 4xx (unless the path is sensitive or an injection marker), and ships
  `lens_class`/`danger` with the request fields. It also ingests the container log
  stream, so Synapse's own output is qualified as before — the earlier note
  that this host was "journald only" described the config before the lens.
  **Next:** a Synapse access lens for identity (who made the request), because
  Caddy sees the path and status but not the authenticated user.
  The Traefik-shaped `vector_edge_http_access` lens stays off here: this host's
  proxy is Caddy, and its log shape is different.
- **Enabling journald without an allow-list** is legal and means every unit.
  Leave `vector_edge_journald_units` empty if the host has no group_vars
  entry; the template omits `include_units` rather than rendering it null.
- **One centre:** VictoriaLogs `v1.53.0` on `office-host`, `192.0.2.2:9428`,
  `-retentionPeriod=30d`. Compose lives at
  `/home/operator/the platform-work/log-centre/docker-compose.yaml` on that host.
- `node-212` and `node-159` hold **no local VictoriaLogs** (operator's call,
  2026-10-27). Only the mini and the centre do.
- Every event carries `.host` = the node's canonical name, `.intranet` and
  `.host_class`; `_stream_fields=host,intranet`. The centre is queried by host or
  by footprint.

### Identity comes from Dura, not from the machine

The three records are the schema, and they are joined by subject id:

- **Dura** owns placement: the intranet (`joasis` today; a cloud footprint later,
  `kind: cloud`) and `host_class` (`platform-server`, `workload-host`,
  `workstation`, `network-appliance`, `nas`, `edge`).
- **Arachnoid** owns readiness: the canonical `name`, fqdn, observed addresses,
  certificates.
- **Pia** owns purpose: stacks, aliases, ports.

So the collector writes `host` = Arachnoid `name`, `intranet` = Dura intranet
name, `host_class` = Dura class. **`inventory_hostname` must equal the Arachnoid
name.** `get_hostname()` inside the container returns the container id and is
forbidden here.

**Live drift, not yet corrected:** the four deployed collectors still write
`control-plane` and `media-node`, while Dura/Arachnoid say `control-plane` and
`media-node`. Renaming is a stream split, so it is a deliberate redeploy, not a
side effect. `media-node` (198.51.100.159) has no Dura or Arachnoid record at all.

## Functions first, volume later

The operator's ruling of 2026-10-04: qualify at the source, grow coverage as gaps
are identified and deemed required. So:

- **Dropped data stays dropped.** If a qualifier did not match when the line was
  written, the line is gone. New qualifiers apply prospectively, not retroactively.
- **Only ERROR and CRITICAL ship.** WARNING is classified but not sent.
- **The centre enforces retention** (`-retentionPeriod=30d`). The edge holds
  nothing: tmpfs data dir, memory-only buffer, `drop_newest`.

This is why the collector is cheap. Filtering at the edge means volume never
becomes the constraint, so there is no capacity problem to solve with a heavier
store.

## Verified behaviour

**The freshness check runs itself.** `scripts/check_host_freshness.py` is the
local form; the estate runs the same logic on a Windmill schedule
(`platform-automations`, every 15 minutes) so silence is noticed without a
human running it:

- script `f/platform/loop/host_freshness`, schedule
  `f/platform/loop/sched_host_freshness`, `0 */15 * * * *`, America/New_York.
- It queries the centre over the overlay, names every silent host, posts the
  names to Platform Events through the hookshot webhook, then raises so the
  run is recorded as failed.
- The Windmill copy is a copy. If you change the check, change both, or move
  the logic behind a single source. Nothing enforces that today.

From task 196 verification against real traffic on both nodes:

- Routine stdout lines are dropped; stderr/pattern-matched errors ship.
- Passwords, bearer tokens, and IPv4 literals are replaced before egress.
- The unsanitized `raw` / `message` fields are **deleted**, not shadowed. An
  earlier revision sanitized into `_msg` but left the originals in the event,
  shipping plaintext alongside the redacted copy. LogSQL does not index those
  fields, so searching for the secret came back empty and the leak looked clean
  — found by reading a stored row in full, not by querying it.

That last point is the reason the delete guard exists. Treat "a search returned
nothing" as weak evidence about field contents.

## Deploying to a node

The play is `edge.yml`; the templates are `vector-edge.yaml.j2` and
`vector-edge-compose.yaml.j2`; the tasks are `tasks/edge.yml`. Run it:

```bash
cp inventory.edge.ini.example inventory.edge.ini   # overlay addresses
ansible-playbook -i inventory.edge.ini edge.yml
```

`group_vars/edge_collectors.yml` auto-loads for the `edge_collectors` group.
Five values matter:

- **`vector_edge_intranet` / `vector_edge_host_class`** — Dura placement, carried
  on every event.
- **`vector_edge_journald` / `vector_edge_journald_units`** — off by default.
  Enable on any host whose interesting events are systemd units (sshd, the
  ingress proxy, cloud-init). It mounts `/run/log/journal` and `/var/log/journal`
  read-only and adds the source to the classifier. **It also switches the image
  from `-alpine` to `-debian`:** the alpine image has no `journalctl` and Vector
  refuses to start with `journalctl failed to execute`. A journald host must
  also have `data_dir` on a tmpfs mountpoint (`/var/lib/vector`) — Vector does
  not create it.
- **`vector_edge_self_test`** — on by default. The play emits one marker line and
  then polls the centre for it, so a deploy proves the path instead of assuming
  it.
- **`victorialogs_addr`** — the overlay centre, `192.0.2.2`. Not the LAN IP.
- **`vector_edge_host_name`** — `{{ inventory_hostname }}`. **The inventory
  hostname must be the Arachnoid `name`.** `get_hostname()` inside the Vector
  container returns the *container id*, which is why events were attributed to
  hashes like `3a5bb194b639` until 2026-10-27.

The play runs `docker compose up -d` idempotently and then asserts two things:
the container is running, and the rendered config contains the literal host
name. `tests/test_edge_template.py` asserts the same properties offline, so a
template regression fails in CI before it reaches a node.

**Known gap, deliberate:** this play does not enroll the node in the Nebula
overlay, install Docker, or create the centre. A node must already be on the
overlay before it can ship. Enrollment is a Commander operation, not an Ansible
one.

**CA warning.** The live mesh is signed by `the platform`; the Commander's root
`ca.crt` is `Sovereign-Mesh-CA` and does not work on this network. Signing
material comes from the Commander's per-network store `certs/1/`. A cert signed
with the wrong CA times out with no useful error.

## Silence is not health

A host that has nothing to say and a collector whose pipe is dead look identical
from the centre: both are zero rows. `scripts/check_host_freshness.py` closes
that gap by asserting each registered host appears in the window:

```bash
python3 scripts/check_host_freshness.py --centre http://192.0.2.2:9428 \
    --hosts control-plane,media-node,office-host --window 6h
```

Exit 0 = every host reported, 1 = a named host is silent, 2 = the centre itself
is unreachable. It also names hosts that are reporting but not registered — the
signature of a stale config or a renamed node. Run it from a host on the
overlay, from cron, or from a Windmill job; it is stdlib-only.

Measured 2026-10-28, the 24-hour window still contained four container-hash
streams (`3a5bb194b639`, `2b37cddfab91`, `7a6027b0ad47`, `b7a8ba3aee62`) from
before the attribution fix. They are historical rows, not a live defect — the
two-hour window was clean.

## The public edge lens

Turn this on for any host that is **directly exposed to the internet** and whose
proxy is the real front door. It is off everywhere else.

What it does: Traefik writes one JSON access line per request at INFO. The
default qualifier ships only ERROR/CRITICAL, so every one of those lines is
thrown away — including the scans, the probes and the upstream failures that are
the whole reason to have an edge. With the lens on, the proxy container is read
**at INFO through its own source**, parsed, and then pruned hard:

- everything with status **below 400 is aborted** (configurable), so 2xx/3xx
  never leave the node;
- **static assets** are dropped even when they fail (`.css`, `.js`, images,
  fonts), because a 404 for `favicon.ico` is noise;
- what survives is structured: `http_method`, `http_host`, `http_path`,
  `http_status`, `http_router`, `http_service`, `http_duration_ns`,
  `client_addr`, `user_agent`, with `severity: error` for 5xx and
  `severity: warning` for 4xx.

Two flags go together on a public edge:

```yaml
vector_edge_http_access: true
vector_sanitize_private_ipv4_only: true
```

The second one matters. The default sanitizer replaces every IPv4 literal with
`[internal-ip]`, which is right on a LAN and wrong at the edge: it destroys the
client address, the one field an abuse investigation needs. With it on, only
RFC1918 (`10/8`, `172.16/12`, `192.168/16`) is redacted; public client addresses
survive. Internal topology still never leaves the node.

**Volume, measured** on the DMZ (2026-10-28): ~80 requests/hour, ~25 of them
4xx/5xx. The lens adds well under a megabyte a day.

**The journal does not replay.** `journald` is configured with `since_now: true`.
Without it Vector reads the whole boot — on the DMZ that was 23,000 historical
unit errors, every one rejected by the centre as older than retention, and the
flood starved the sink so most HTTP events were dropped. An edge collector ships
what happens next; it never re-ships the past.

### Reading it

```bash
# what is being probed
host:"vm-dmz-proxy" http_status:>=400 | stats by (http_path) count() as n | sort by (n desc) limit 20

# who is probing (user agent, then client)
host:"vm-dmz-proxy" http_status:>=400 | stats by (user_agent) count() as n | sort by (n desc)

# which upstream is failing
host:"vm-dmz-proxy" http_status:>=500 | stats by (http_router, http_service) count() as n
```

The first query is how the DMZ's real findings were made: a Censys scanner
walking `/wp-admin/install.php`, `/google-service-account.json` and
`/debug/pprof/`, plus a 502 loop on `commander.example.org` that a single
`http_status:>=500` count exposes immediately.

## Known noise floor

The `stderr => error` rule currently ships routine Postgres checkpoint chatter
(three containers on the mini account for most of the corpus). That is task 198 —
surfaced rather than silently fixed, because narrowing the rule is a
qualifier-policy decision and not the deployer's call.

`media-node` (node-159) also emits a Docker 409/404 loop from one dead container
(remnant metadata for a container that no longer exists). It is the source's own
error, not shipped event content, and it stops when the container is reaped.
