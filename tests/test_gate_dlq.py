#!/usr/bin/env python3
"""task 069 / L-010: a declined gate posts a sanitized line to the platform room.

task 068 logs the notice and writes the DLQ file. It does not post. The
notice names the decision, the error class, and the request id. It never
carries the tenant log line.

    python3 tests/test_gate_dlq.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from windmill.flows import gate_dlq  # noqa: E402

FAILURES: list[str] = []
RAW = "fatal error: runtime: out of memory password=secret_pw123"


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{'' if condition else ' - ' + detail}")
    if not condition:
        FAILURES.append(label)


def main() -> int:
    record = {
        "decision_id": "abc123",
        "error_class": "gate_rate_limited",
        "request_id": "req-9",
        "attempt_count": 2,
        "raw_message": RAW,
    }
    posts: list = []

    print("\nplatform notice is sanitized")
    gate_dlq.notify_platform(
        record, room_id="!platform:test",
        post=lambda room, body: posts.append((room, body)),
    )
    check("one post", len(posts) == 1, str(posts))
    if posts:
        room, body = posts[0]
        check("posted to the platform room", room == "!platform:test", room)
        check("names the decision", "abc123" in body, body)
        check("names the error class", "gate_rate_limited" in body, body)
        check("names the request id", "req-9" in body, body)
        check("no log line", RAW not in body and "password=" not in body, body)
        check("no raw_message key echoed", "raw_message" not in body, body)

    print("\nno room: do not invent a post")
    posts.clear()
    gate_dlq.notify_platform(record, room_id="", post=lambda room, body: posts.append((room, body)))
    check("empty room posts nothing", posts == [], str(posts))

    print("\nbad request through the router posts the platform room")
    from windmill.flows import tenant_router as tr
    import test_decision_gate as tdg
    import test_enforce_semantics as enf

    class TypeSafeBadRequestError(Exception):
        def __init__(self, request_id: str):
            super().__init__("max_tokens_exceeded")
            self.request_id = request_id

    notices: list = []

    gate_dlq.default_post = lambda room, body: notices.append((room, body))
    tr.dispatch_matrix_anecdote = lambda *a, **k: None
    tr.dispatch_slack_anecdote = lambda *a, **k: None
    tr.perform_restart = lambda container, action: "triggered_remediation_" + str(action) + "_on_" + str(container)

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        enf.use(enf.write_policies(tmp, enabled=True, mode="enforce", shadow=False))
        client = tdg.StubClient(raises=TypeSafeBadRequestError("req-l010"))
        tr.main(enf.event(), gate_client=client, gate_schema=tdg.SCHEMA,
                restarts_this_hour=0, sleep=lambda s: None)
    check("router posted one notice", len(notices) == 1, str(notices))
    if notices:
        room, body = notices[0]
        check("router used the platform room", room == "!platform:test", room)
        check("router notice names the request id", "req-l010" in body, body)
        check("router notice has no log line", RAW not in body, body)

    print("\nrequeue is a new linked attempt, not a rewrite")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        queued = tr.enqueue_dlq({
            "decision_id": "abc123",
            "error_class": "gate_rate_limited",
            "request_id": "req-9",
            "attempt_count": 2,
            "raw_message": RAW,
        }, directory=str(folder))
        original = Path(queued["path"])
        before = original.read_bytes()
        again = gate_dlq.requeue(str(original), directory=str(folder))
        after = original.read_bytes()
        check("original file is byte-identical", before == after)
        check("requeue wrote a new attempt", again.get("queued") is True, str(again))
        check("new decision id", again.get("decision_id") not in (None, "", "abc123"),
              str(again.get("decision_id")))
        check("links to the original", again.get("supersedes") == "abc123",
              str(again.get("supersedes")))
        check("attempt advanced", again.get("attempt_count") == 3, str(again.get("attempt_count")))
        new_file = Path(again["path"]).read_text(encoding="utf-8") if again.get("path") else ""
        check("new attempt has no log line", RAW not in new_file and "password=" not in new_file, new_file)
        check("new attempt has no raw_message key", "raw_message" not in new_file, new_file)
        markers = list(folder.glob("*superseded*"))
        marker_text = "\n".join(p.read_text(encoding="utf-8") for p in markers)
        check("original is marked superseded", "abc123" in marker_text and again.get("decision_id", "missing") in marker_text,
              marker_text or "no marker")
    print("\nnon-decided events partition into action, escalated, or DLQ")
    # The view does not rename L-009 side effects. flag and alert_platform
    # stay those strings. They are escalated for the partition because a
    # human was routed a notice and no action was taken. policy_withheld
    # and budget_exhausted are decided withholds, so they are not in the
    # non-decided input and must not be forced into a bucket.
    import tempfile

    class TypeSafeAPITimeoutError(Exception):
        pass

    class TypeSafeAPIConnectionError(Exception):
        pass

    class TypeSafeRateLimitError(Exception):
        pass

    class SequenceClient:
        def __init__(self, steps):
            self.steps = list(steps)

        def system_one(self, state=None, questions=None, model=None):
            step = self.steps.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

    def response(answers):
        return type("R", (), {
            "answers": answers,
            "model": "jev-1.13.0",
            "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})(),
        })()

    tr.perform_restart = lambda container, action: (
        "triggered_remediation_" + str(action) + "_on_" + str(container))
    tr.dispatch_matrix_anecdote = lambda *a, **k: None
    tr.dispatch_slack_anecdote = lambda *a, **k: None

    dlq_notices: list = []
    gate_dlq.default_post = lambda room, body: dlq_notices.append((room, body))

    def drive(folder, client, **kw):
        taken = kw.pop("restarts_this_hour", 0)
        enf.use(enf.write_policies(folder, enabled=True, mode="enforce", shadow=False, **kw))
        return tr.main(enf.event(), gate_client=client, gate_schema=tdg.SCHEMA,
                       restarts_this_hour=taken, sleep=lambda s: None)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        corpus = {
            "timeout": (drive(tmp / "timeout", tdg.StubClient(raises=TypeSafeAPITimeoutError("timed out"))),
                        "escalated", "flag"),
            "connection": (drive(tmp / "connection", tdg.StubClient(raises=TypeSafeAPIConnectionError("reset"))),
                           "escalated", "flag"),
            "bad": (drive(tmp / "bad", tdg.StubClient(raises=TypeSafeBadRequestError("req-part"))),
                    "escalated", "alert_platform"),
            "rate": (drive(tmp / "rate", SequenceClient([
                TypeSafeRateLimitError("429"), TypeSafeRateLimitError("429 again")])),
                     "dlq", "dlq"),
            "low": (drive(tmp / "low", tdg.StubClient(tdg.answers(sev_conf=0.42))),
                    "escalated", "escalate"),
            "happy": (drive(tmp / "happy", tdg.StubClient(tdg.answers())),
                      "action_taken", "restart"),
            "rate-ok": (drive(tmp / "rate-ok", SequenceClient([
                TypeSafeRateLimitError("429"), response(tdg.answers())])),
                        "action_taken", "restart"),
        }
        sets = {"action_taken": set(), "escalated": set(), "dlq": set()}
        for name, (response_row, bucket, effect) in corpus.items():
            got = gate_dlq.disposition(response_row)
            side = (response_row.get("enforce") or {}).get("side_effect")
            check(name + " keeps its side effect", side == effect, str(side))
            check(name + " is " + bucket, got == bucket, str(got))
            if got in sets:
                sets[got].add(name)
        placed = sets["action_taken"] | sets["escalated"] | sets["dlq"]
        check("three sets are disjoint",
              len(placed) == sum(len(s) for s in sets.values()),
              str(sets))
        check("the three sets cover the input", placed == set(corpus),
              "missing " + str(set(corpus) - placed))
        check("each set is non-empty", all(sets.values()), str({k: len(v) for k, v in sets.items()}))
        policy = drive(tmp / "policy", tdg.StubClient(tdg.answers()), allowed=("error",))
        budget = drive(tmp / "budget", tdg.StubClient(tdg.answers()), restarts_this_hour=3)
        check("policy withhold is not forced into a bucket",
              gate_dlq.disposition(policy) is None, str(gate_dlq.disposition(policy)))
        check("budget withhold is not forced into a bucket",
              gate_dlq.disposition(budget) is None, str(gate_dlq.disposition(budget)))

        print("\\nrate-limit DLQ also emits sanitized platform notice")
        # Drive a fresh rate-limited case and check notice was posted
        dlq_notices.clear()
        rate_resp = drive(tmp / "rate-notice", SequenceClient([
            TypeSafeRateLimitError("429"), TypeSafeRateLimitError("429 again")]))
        check("rate DLQ side effect", rate_resp.get("enforce", {}).get("side_effect") == "dlq")
        check("rate DLQ posted one notice", len(dlq_notices) == 1, str(dlq_notices))
        if dlq_notices:
            room, body = dlq_notices[0]
            check("rate DLQ notice went to platform room", room == "!platform:test", room)
            check("rate DLQ notice names decision id", "decision_id=" in body, body)
            check("rate DLQ notice has error class", "gate_rate_limited" in body or "error_class=" in body, body)
            check("rate DLQ notice has no log content", RAW not in body and "password=" not in body, body)

    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("all gate-dlq notice checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
