"""task 069 / L-010: a declined gate is a notice, and a requeue is a new attempt.

Stage 1 stays a folder of files. This module owns the sanitized platform
notice and the manual requeue. It does not tag or index. Those are later
slices.

The notice body is an allow-list. A field not named here is not posted.
A room id is enough to post: the default poster uses the same homeserver
and token as the shadow ledger, and a missing credential skips rather than
inventing a send.

Requeue writes a new file. It does not rewrite the original. The original
is marked superseded by a sibling marker, so the history file stays
byte-identical.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

NOTICE_FIELDS = (
    "decision_id",
    "error_class",
    "reason_code",
    "request_id",
    "attempt_count",
    "tenant_id",
)
MAX_LINE = 200


# A view over L-009 side effects. It does not rename them.
# flag and alert_platform are escalated: a human was routed a notice and
# no action was taken. policy_withheld and budget_exhausted are decided
# withholds, so they stay outside the three sets.
_ESCALATED = frozenset({"escalate", "flag", "alert_platform"})


def disposition(response):
    """One of action_taken, escalated, dlq, or None. Never a second bucket."""
    if not isinstance(response, dict):
        return None
    effect = (response.get("enforce") or {}).get("side_effect")
    if effect == "restart":
        return "action_taken"
    if effect in _ESCALATED:
        return "escalated"
    if effect == "dlq":
        return "dlq"
    return None


def notice_body(record: dict | None) -> str:
    """One line a human can route. Never the tenant log. Bounded."""
    rec = record or {}
    parts = ["gate declined"]
    for key in NOTICE_FIELDS:
        value = rec.get(key)
        if value is None or value == "":
            continue
        text = str(value).replace("\n", " ").replace("\r", " ")
        parts.append(f"{key}={text}")
    return " ".join(parts)[:MAX_LINE]


def default_post(room_id, body):
    """Best-effort Matrix send. Never raises. No token, no send."""
    try:
        import requests
        base = os.environ.get("SYNAPSE_HOMESERVER_URL", "")
        token = os.environ.get("GATUS_MATRIX_TOKEN", "")
        if not base or not token or not room_id:
            return {"status": "skipped"}
        url = (f"{base}/_matrix/client/r0/rooms/{room_id}/send/m.room.message"
               f"?access_token={token}")
        requests.post(url, json={"msgtype": "m.text", "body": body}, timeout=5)
        return {"status": "sent"}
    except Exception as exc:  # noqa: BLE001 - a notice must not abort dispatch
        return {"status": "error", "error": type(exc).__name__}


def _linked_id(original_id: str, attempt: int) -> str:
    """A new id. The payload names the original, so this is not a replay."""
    payload = json.dumps(
        {"supersedes": original_id, "attempt": attempt},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def requeue(path, *, directory=None):
    """New linked attempt. The original file is not opened for write.

    A missing file, a bad file, or a missing decision id returns queued false
    and writes nothing. The new record is the allow-list plus the link.
    raw_message is not copied even if the source file has it.
    """
    source = Path(path)
    try:
        record = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {"queued": False}
    if not isinstance(record, dict):
        return {"queued": False}
    original_id = record.get("decision_id")
    if not original_id:
        return {"queued": False}
    try:
        attempt = int(record.get("attempt_count") or 0) + 1
    except (TypeError, ValueError):
        return {"queued": False}
    new_id = _linked_id(str(original_id), attempt)
    fresh = {
        "decision_id": new_id,
        "supersedes": str(original_id),
        "attempt_count": attempt,
        "error_class": record.get("error_class"),
        "request_id": record.get("request_id"),
        "tenant_id": record.get("tenant_id"),
    }
    folder = directory or str(source.parent)
    from windmill.flows.tenant_router import enqueue_dlq
    written = enqueue_dlq(fresh, directory=folder)
    if not written.get("queued"):
        return written
    stem = "".join(c for c in str(original_id) if c.isalnum()) or "undecided"
    marker = Path(folder) / (stem + ".superseded")
    line = "superseded decision_id=%s by=%s\n" % (original_id, new_id)
    try:
        # Append. A second requeue adds a line. It never rewrites the original.
        with marker.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        written["queued"] = False
        written["marked"] = False
        return written
    written["marked"] = True
    return written


def notify_platform(record, room_id, post=None):
    """Post the sanitized line. No room, no post. A poster failure does not raise."""
    if not room_id:
        return None
    body = notice_body(record)
    sender = post or default_post
    try:
        sender(room_id, body)
    except Exception:  # noqa: BLE001 - same contract as the default poster
        return None
    return body
