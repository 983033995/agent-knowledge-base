#!/usr/bin/python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import lifecycle


SOURCES = ("qoder", "cursor", "codex", "workbuddy")
EVENTS = ("start", "stop", "end")


def _reject_constant(value: str) -> None:
    raise ValueError("non-JSON constant")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _absolute_path(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value and Path(value).is_absolute()


def normalize_event(source: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    event = dict(payload)
    if source == "cursor" and "workspace_roots" in event:
        roots = event["workspace_roots"]
        if not isinstance(roots, list) or len(roots) != 1 or not _absolute_path(roots[0]):
            return None
        event["cwd"] = roots[0]
    if not _absolute_path(event.get("cwd")):
        return None
    return event


def run(source: str, event_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    event = normalize_event(source, payload)
    if event is None:
        return None
    if event_name == "start":
        context = lifecycle.recall(event["cwd"])
        if not context:
            return None
        if source == "cursor":
            return {"additional_context": context}
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    if not _absolute_path(event.get("transcript_path")):
        return None
    # stop_hook_active is not a capture guard: no follow-up is ever requested.
    lifecycle.capture(source, event)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local knowledge lifecycle hook")
    parser.add_argument("source", choices=SOURCES)
    parser.add_argument("event", choices=EVENTS)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.read(), parse_constant=_reject_constant, object_pairs_hook=_object)
        if not isinstance(payload, dict):
            return 0
        output = run(args.source, args.event, payload)
        if output is not None:
            print(json.dumps(output, ensure_ascii=False))
    except Exception:
        # Never print exception messages, paths, payloads or transcript contents.
        print("akb lifecycle hook: input or processing failed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
