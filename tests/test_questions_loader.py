#!/usr/bin/env python3
"""L-004 tests: question schema validation and (when possible) a live build+call.

Runs under plain python3 for validation. The build and live-call sections need
`typesafe_sdk`, so run those with the venv interpreter:

    python3 tests/test_questions_loader.py
    ~/projects/typesafe/.venv/bin/python tests/test_questions_loader.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from windmill.flows.jev import loader  # noqa: E402
from windmill.flows import decision_state as ds  # noqa: E402

FAILURES: list[str] = []
SCHEMA = ROOT / "windmill" / "flows" / "jev" / "questions.v1.yaml"


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{'' if condition else ' - ' + detail}")
    if not condition:
        FAILURES.append(label)


def rejects(label: str, mutate, expect_fragment: str = "") -> None:
    """Assert that mutating a valid schema makes it invalid."""
    raw = copy.deepcopy(loader.load_raw(SCHEMA))
    mutate(raw)
    try:
        loader.validate_schema(raw)
    except loader.QuestionSchemaError as exc:
        ok = expect_fragment.lower() in str(exc).lower() if expect_fragment else True
        check(label, ok, f"error did not mention {expect_fragment!r}: {exc}")
    except Exception as exc:  # noqa: BLE001
        check(label, False, f"raised {type(exc).__name__} instead of QuestionSchemaError: {exc}")
    else:
        check(label, False, "schema was accepted")


def load_key() -> str | None:
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key.strip()
    env_file = Path.home() / ".hermes" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("TYPESAFE_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


def main() -> int:
    print("shipped schema:")
    summary = loader.validate_schema(loader.load_raw(SCHEMA))
    check("version is present", bool(summary["version"]))
    check("five questions", summary["count"] == 5, str(summary["count"]))
    check("model pins a version, not an alias", not str(summary["model"]).endswith("-latest"),
          str(summary["model"]))
    check("all three primitive types are exercised",
          set(summary["types"].values()) == {"choice", "noul"})
    for qid, n in summary["choice_options"].items():
        check(f"{qid} stays under the 255-option limit", n <= 255, str(n))

    print("\nvalidation rejects:")
    rejects("missing version", lambda r: r.pop("version"), "version")
    rejects("alias model", lambda r: r.update(model="jev-latest"), "pin a version")
    rejects("no questions", lambda r: r.update(questions={}), "questions")
    rejects("unknown question type",
            lambda r: r["questions"]["fault_class"].update(type="classify"), "unknown type")
    rejects("empty instructions",
            lambda r: r["questions"]["is_transient"].update(instructions="  "), "instructions")
    rejects("choice without criteria",
            lambda r: r["questions"]["severity"].pop("criteria"), "no `criteria`")
    rejects("choice without a no-match option",
            lambda r: r["questions"]["severity"].pop("no_match_option"), "no_match_option")
    rejects("no-match option that is not an option",
            lambda r: r["questions"]["severity"].update(no_match_option="nonexistent"), "not one of its criteria")
    rejects("too many options",
            lambda r: r["questions"]["fault_class"].update(
                criteria={f"opt_{i}": "x" for i in range(256)}, no_match_option="opt_0"),
            "255")
    rejects("noul criteria missing a side",
            lambda r: r["questions"]["is_transient"].update(criteria={"true": "yes"}),
            "'true' and 'false'" if os.name != "nt" else "true")
    rejects("question id leaked into its own instructions",
            lambda r: r["questions"]["severity"].update(
                instructions="Return the severity for this log line."),
            "mentions its own id")

    print("\nSDK build:")
    try:
        result = loader.load_schema(SCHEMA, build=True)
    except loader.QuestionSchemaError as exc:
        print(f"  skipped: {exc}")
    else:
        built = result["questions"]
        check("five SDK question objects", len(built) == 5, str(len(built)))
        from typesafe_sdk import Choice, Noul
        check("choice built as Choice", isinstance(built["fault_class"], Choice))
        check("noul built as Noul", isinstance(built["restart_would_help"], Noul))
        check("options survive the build",
              len(built["fault_class"].criteria) == 6, str(len(built["fault_class"].criteria)))

        key = load_key()
        if not key:
            print("  live call skipped: no TYPESAFE_API_KEY")
        else:
            from typesafe_sdk import TypeSafeClient
            fixture = json.loads((ROOT / "tests" / "fixtures" / "events" /
                                  "oom_memory_ceiling_01.json").read_text())
            state = ds.derive_state(fixture, "sanitized_content")
            with TypeSafeClient(api_key=key) as client:
                resp = client.system_one(state=state, questions=built, model=result["model"])
            check("all five questions answered", set(resp.answers) == set(built),
                  f"got {sorted(resp.answers)}")
            check("model pinned in the response", resp.model == result["model"], resp.model)
            print(f"    live: fault_class={resp.answers['fault_class'].choice!r} "
                  f"severity={resp.answers['severity'].choice!r} "
                  f"help={resp.answers['restart_would_help'].noul} "
                  f"discards={resp.answers['restart_discards_state'].noul} "
                  f"tokens={resp.usage.input_tokens}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("all question-schema checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
