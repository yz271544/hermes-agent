#!/usr/bin/env python3
"""Dependency-free structural validation for a generated ontology bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

TTL_FILES = [
    "01-core.owl.ttl",
    "02-domain-metric.ttl",
    "03-scenario.ttl",
    "04-data-capability.ttl",
    "05-lexicon-skos.ttl",
    "06-mapping.r2rml.ttl",
    "07-shapes.ttl",
]
REPORT_FILES = [
    "reports/source-inventory.md",
    "reports/assumptions.md",
    "reports/release-plan.md",
]
MANIFEST_KEYS = {
    "bundle_id", "bundle_version", "generated_at", "base_iri",
    "target_graph", "operating_mode", "sources", "artifacts",
    "assertion_counts", "publication",
}
STATUSES = {"confirmed", "inferred", "candidate", "unresolved"}
SECRET_PATTERNS = [
    re.compile(r"(?i)(password|passwd|api[_-]?key|authorization)\s*[:=]\s*[^\s\"']+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._-]{12,}"),
    re.compile(r"(?i)postgres(?:ql)?://[^\s/@:]+:[^\s/@]+@"),
]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            value.update(chunk)
    return value.hexdigest()


def issue(items: list[dict[str, str]], code: str, message: str, path: Path) -> None:
    items.append({"code": code, "message": message, "path": str(path)})


def validate(root: Path) -> dict[str, object]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    hashes: dict[str, str] = {}

    required = [root / "manifest.json"] + [root / name for name in TTL_FILES + REPORT_FILES]
    for path in required:
        if not path.is_file():
            issue(errors, "missing_file", "Required file is missing", path)
        elif path.stat().st_size == 0:
            issue(errors, "empty_file", "Required file is empty", path)
        else:
            hashes[str(path.relative_to(root))] = digest(path)

    manifest_path = root / "manifest.json"
    manifest: dict[str, object] = {}
    if manifest_path.is_file() and manifest_path.stat().st_size:
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("manifest root must be an object")
            manifest = loaded
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            issue(errors, "invalid_manifest", str(exc), manifest_path)

    missing_keys = sorted(MANIFEST_KEYS - manifest.keys())
    if missing_keys:
        issue(errors, "manifest_keys", "Missing keys: " + ", ".join(missing_keys), manifest_path)

    counts = manifest.get("assertion_counts")
    if isinstance(counts, dict):
        missing_statuses = sorted(STATUSES - counts.keys())
        if missing_statuses:
            issue(errors, "assertion_statuses", "Missing statuses: " + ", ".join(missing_statuses), manifest_path)
        for key, value in counts.items():
            if key in STATUSES and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                issue(errors, "assertion_count", "Invalid count for " + key, manifest_path)
    elif manifest:
        issue(errors, "assertion_counts", "assertion_counts must be an object", manifest_path)

    publication = manifest.get("publication")
    if manifest.get("operating_mode") == "offline" and isinstance(publication, dict):
        if publication.get("status") not in {None, "not_attempted", "offline_validated"}:
            issue(errors, "offline_publication", "Offline bundles cannot claim connected publication", manifest_path)

    artifacts = manifest.get("artifacts")
    entries = artifacts if isinstance(artifacts, dict) else {
        item.get("path", ""): item for item in artifacts if isinstance(item, dict)
    } if isinstance(artifacts, list) else {}
    for name in TTL_FILES:
        artifact = entries.get(name)
        if not isinstance(artifact, dict):
            issue(errors, "artifact_missing", "Missing artifact contract: " + name, manifest_path)
        else:
            if artifact.get("sha256") != hashes.get(name):
                issue(errors, "artifact_hash", "Artifact hash differs: " + name, manifest_path)
            if type(artifact.get("triples")) is not int or artifact["triples"] < 0:
                issue(errors, "artifact_triples", "Invalid triples count: " + name, manifest_path)

    from urllib.parse import urlparse
    base = urlparse(str(manifest.get("base_iri", "")))
    if base.scheme != "https" or base.netloc != "arcp.sxmcc.com":
        issue(errors, "base_iri", "Expected https://arcp.sxmcc.com/ project namespace", manifest_path)
    if manifest.get("target_graph") != "https://arcp.sxmcc.com/tenant/chatbi":
        issue(errors, "target_graph", "Expected the ChatBI tenant graph", manifest_path)

    checked: set[Path] = set()
    for name in TTL_FILES:
        path = root / name
        if not path.is_file() or not path.stat().st_size:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            issue(errors, "ttl_read", str(exc), path)
            continue
        checked.add(path)
        if text.count("{") != text.count("}"):
            issue(warnings, "brace_balance", "Raw braces differ; only an RDF parser can distinguish literals/comments", path)
        if text.count("[") != text.count("]"):
            issue(warnings, "bracket_balance", "Raw brackets differ; parse RDF before release", path)
        if name == "04-data-capability.ttl" and re.search(r"(?i)insert\s+into|delete\s+from|update\s+\w+\s+set", text):
            issue(errors, "mutation_sql", "Mutation SQL appears in the capability graph", path)

    for relpath in hashes:
        path = root / relpath
        if path.suffix not in {".md", ".json", ".ttl", ".yaml", ".yml"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                issue(errors, "possible_secret", "Possible credential found", path)

    return {
        "ok": not errors,
        "root": str(root),
        "errors": errors,
        "warnings": warnings,
        "sha256": hashes,
        "note": "Structural validation only; parse RDF with a standards-compliant parser before release.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="Path to the generated ontology bundle")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()

    root = args.bundle.expanduser().resolve()
    if not root.is_dir():
        report: dict[str, object] = {
            "ok": False,
            "errors": [{"code": "bundle_dir", "message": "Bundle directory does not exist", "path": str(root)}],
        }
    else:
        report = validate(root)

    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
