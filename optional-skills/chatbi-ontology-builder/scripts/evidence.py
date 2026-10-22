"""Shared, local evidence envelope. No external model calls or database writes."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def redact(text: str) -> str:
    text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    return re.sub(
        r'''(?ix)((?:["']?)(?:password|passwd|secret|token|api_key|access_key|secret_key)(?:["']?)\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;<>]+)''',
        r'\1"[REDACTED]"', text,
    )


def redact_values(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_values(item) for item in value]
    return value


def block(source_id: str, kind: str, locator: dict, content: object) -> dict:
    serialized = json.dumps([locator, content], ensure_ascii=False, sort_keys=True, default=str)
    return {"block_id": source_id + ":" + sha256(serialized.encode())[:20],
            "block_type": kind, "locator": locator, "content": content}


def envelope(source_id: str, kind: str, identity: dict, blocks: list, coverage: dict) -> dict:
    return {"evidence_version": "1.0", "source_id": source_id, "source_type": kind,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "identity": identity, "coverage": coverage, "blocks": blocks}


def write_new(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")


def load_config(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a JSON object")
    return data
