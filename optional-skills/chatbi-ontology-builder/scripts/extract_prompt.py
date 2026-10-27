#!/usr/bin/env python3
"""Capture user-provided definitions/requirements as located evidence, not verified facts."""
import argparse
import json
from pathlib import Path
import sys

from evidence import block, envelope, redact, sha256, write_new


def extract(path, source_id, authority="instruction", author=None, message_id=None):
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig")
    blocks, paragraph, start = [], [], 1
    lines = text.splitlines()
    for number, line in enumerate([*lines, ""], 1):
        if line.strip():
            if not paragraph:
                start = number
            paragraph.append(line)
        elif paragraph:
            locator = {"path": str(Path(path).resolve()), "line_start": start, "line_end": number - 1,
                       "sha256": sha256(raw), "message_id": message_id}
            blocks.append(block(source_id, "user_statement", locator,
                                {"text": redact("\n".join(paragraph)), "declared_role": authority,
                                 "author": author, "verification": "requires_reconciliation"}))
            paragraph = []
    return envelope(source_id, "prompt", {"path": str(Path(path).resolve()), "sha256": sha256(raw),
                    "author": author, "declared_role": authority}, blocks,
                    {"complete": bool(blocks), "issues": [] if blocks else ["empty_prompt"],
                     "semantic_status": "user_provided; not automatically confirmed"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--authority", choices=("instruction", "business_definition", "owner_decision"), default="instruction")
    parser.add_argument("--author")
    parser.add_argument("--message-id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = extract(args.input, args.source_id, args.authority, args.author, args.message_id)
        write_new(args.output, result)
        print(json.dumps({"blocks": len(result["blocks"]), "output": str(args.output)}))
        return 0 if result["coverage"]["complete"] else 2
    except (ValueError, OSError) as exc:
        print("Prompt extraction failed: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
