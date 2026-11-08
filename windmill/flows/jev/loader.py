#!/usr/bin/env python3
"""L-004: load and validate versioned decision-gate questions.

Questions live in `questions.v1.yaml`, not in Python, so wording changes without a
code change and `questions_version` can be recorded in every audit envelope.

Validation is deliberately independent of the SDK: `validate_schema()` runs under
plain python3 (handy in CI and in this repo's other test scripts), while
`build_questions()` imports `typesafe_sdk` lazily so tests that never make a call
do not need it installed.

Usage:
    python3 windmill/flows/jev/loader.py --check          # validate the shipped schema
    python3 windmill/flows/jev/loader.py --build          # also build SDK objects
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

DEFAULT_SCHEMA = Path(__file__).resolve().parent / "questions.v1.yaml"
VALID_TYPES = ("choice", "score", "noul")
MAX_CHOICE_OPTIONS = 255
# A Choice needs an explicit "none of these" outcome: probability lands somewhere
# even when nothing fits, so the schema must name that somewhere.
NO_MATCH_REQUIRED = True


class QuestionSchemaError(ValueError):
    """Raised when a question schema is unusable. Never downgraded to a warning."""


def load_raw(path: Path | str | None = None) -> dict:
    path = Path(path or DEFAULT_SCHEMA)
    if not path.exists():
        raise QuestionSchemaError(f"schema not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise QuestionSchemaError(f"{path} is not a mapping")
    return raw


def validate_schema(raw: dict) -> dict:
    """Validate structure and return a summary. Raises QuestionSchemaError."""
    version = raw.get("version")
    if not version:
        raise QuestionSchemaError("schema has no `version`")
    if raw.get("model", "").endswith("-latest"):
        raise QuestionSchemaError(
            "`model` must pin a version, not an alias: a moving model invalidates calibrated thresholds"
        )

    questions = raw.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise QuestionSchemaError("schema has no `questions` mapping")

    for qid, q in questions.items():
        where = f"question {qid!r}"
        if not isinstance(q, dict):
            raise QuestionSchemaError(f"{where} is not a mapping")

        qtype = q.get("type")
        if qtype not in VALID_TYPES:
            raise QuestionSchemaError(f"{where} has unknown type {qtype!r}; expected one of {VALID_TYPES}")

        instructions = q.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise QuestionSchemaError(
                f"{where} has no `instructions` - ids are not sent to the model, so the whole "
                f"question must live in the instructions text"
            )

        criteria = q.get("criteria")

        if qtype == "choice":
            if not isinstance(criteria, dict) or not criteria:
                raise QuestionSchemaError(f"{where} is a choice with no `criteria`")
            if len(criteria) > MAX_CHOICE_OPTIONS:
                raise QuestionSchemaError(
                    f"{where} declares {len(criteria)} options; the API rejects more than "
                    f"{MAX_CHOICE_OPTIONS} choices"
                )
            if NO_MATCH_REQUIRED:
                no_match = q.get("no_match_option")
                if not no_match:
                    raise QuestionSchemaError(
                        f"{where} is a choice with no `no_match_option`; an unmatched input "
                        f"still lands on some option, so name the one that means 'none of these'"
                    )
                if no_match not in criteria:
                    raise QuestionSchemaError(
                        f"{where} declares no_match_option {no_match!r}, which is not one of its criteria"
                    )

        if qtype == "score":
            if not isinstance(criteria, list) or not criteria:
                raise QuestionSchemaError(f"{where} is a score whose `criteria` must be an ordered list")
            if len(criteria) > MAX_CHOICE_OPTIONS:
                raise QuestionSchemaError(f"{where} declares {len(criteria)} levels; max is {MAX_CHOICE_OPTIONS}")

        if qtype == "noul" and criteria is not None:
            if not isinstance(criteria, dict) or set(criteria) != {"true", "false"}:
                raise QuestionSchemaError(
                    f"{where} is a noul whose optional `criteria` must have exactly the keys "
                    f"'true' and 'false'"
                )

        # The question must stand alone: if the id leaks into the text, the author was
        # relying on the model seeing the key, which it never does.
        haystack = instructions + "\n" + yaml.safe_dump(criteria or {}, sort_keys=True)
        if re.search(r"\b%s\b" % re.escape(qid), haystack):
            raise QuestionSchemaError(
                f"{where} mentions its own id inside instructions or criteria; ids are not sent "
                f"to the model, so this text is meaningless to it"
            )

    return {
        "version": version,
        "model": raw.get("model"),
        "count": len(questions),
        "types": {qid: q["type"] for qid, q in questions.items()},
        "choice_options": {qid: len(q.get("criteria", {}) or {}) for qid, q in questions.items()
                           if q["type"] == "choice"},
    }


def build_questions(raw: dict) -> dict:
    """Turn validated schema entries into SDK question objects. Requires typesafe_sdk."""
    try:
        from typesafe_sdk import Choice, Noul, Score
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise QuestionSchemaError(
            "typesafe_sdk is not importable; install windmill/flows/jev/requirements.txt "
            f"(underlying error: {exc})"
        ) from exc

    built = {}
    for qid, q in raw["questions"].items():
        # `purpose` and `no_match_option` are ours, not the API's; only type,
        # instructions, and criteria are sent.
        kwargs = {"instructions": q["instructions"]}
        if q.get("criteria") is not None:
            kwargs["criteria"] = q["criteria"]
        if q["type"] == "choice":
            built[qid] = Choice(**kwargs)
        elif q["type"] == "score":
            built[qid] = Score(**kwargs)
        else:
            built[qid] = Noul(**kwargs)
    return built


def load_schema(path: Path | str | None = None, build: bool = False) -> dict:
    """Load, validate, and optionally build. This is what callers should use."""
    raw = load_raw(path)
    summary = validate_schema(raw)
    return {
        "version": summary["version"],
        "model": summary["model"],
        "summary": summary,
        "questions": build_questions(raw) if build else None,
        "raw": raw,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate decision-gate question schemas")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--check", action="store_true", help="validate only")
    parser.add_argument("--build", action="store_true", help="also build SDK objects")
    args = parser.parse_args(argv)

    try:
        result = load_schema(args.schema, build=args.build)
    except QuestionSchemaError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    s = result["summary"]
    print(f"valid: version={s['version']} model={s['model']} questions={s['count']}")
    for qid, qtype in s["types"].items():
        extra = f" ({s['choice_options'][qid]} options)" if qid in s["choice_options"] else ""
        print(f"  {qid}: {qtype}{extra}")
    if args.build:
        print(f"built {len(result['questions'])} SDK question(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
