#!/usr/bin/env python3
"""Prepare bounded multi-source review materials; do not invent business definitions or TTL."""
import argparse
import json
from pathlib import Path
import sys

from evidence import load_config, sha256, write_new


def prepare(paths, output, max_blocks=5000, max_candidates=10000, max_bytes=50_000_000):
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    if min(max_blocks, max_candidates, max_bytes) < 1:
        raise ValueError("Budgets must be positive")
    if sum(Path(p).stat().st_size for p in paths) > max_bytes:
        raise ValueError("Evidence budget exceeded; use scoped evidence exports")
    sources, seen, ledger, candidates, issues = {}, set(), [], [], []
    for path in paths:
        data = load_config(path)
        sid = data["source_id"]
        if sid in sources:
            raise ValueError("Duplicate source_id; select one version or use distinct source identities")
        sources[sid] = {"source_id": sid, "type": data["source_type"], "path": str(Path(path).resolve()),
                        "sha256": sha256(Path(path).read_bytes()), "coverage": data["coverage"],
                        "authority": "supporting; evaluate per assertion"}
        if not data["coverage"].get("complete"):
            issues.append({"source_id": sid, "kind": "incomplete_source", "details": data["coverage"]})
        for item in data["blocks"]:
            bid = item["block_id"]
            if bid in seen:
                raise ValueError("Duplicate block_id")
            seen.add(bid)
            if len(seen) > max_blocks:
                raise ValueError("Review block budget exceeded; narrow ETL export/document scope")
            entry = {"block_id": bid, "source_id": sid, "kind": item["block_type"],
                     "locator": item["locator"], "disposition": "pending_semantic_review"}
            content = item["content"]
            ledger.append(entry)
            if item["block_type"] == "etl_sql_evidence":
                scopes = {s["scope_id"]: s for s in content.get("scopes", [])}
                for transform in content.get("transformations", []):
                    if len(candidates) >= max_candidates:
                        raise ValueError("Candidate budget exceeded; narrow ETL exports")
                    scope = scopes.get(transform["scope_id"], {})
                    candidates.append({"kind": "etl_output_candidate", "status": "candidate",
                        "evidence": [bid], "source_id": sid, "engine": content.get("engine"),
                        "target_table": transform.get("target_table"),
                        "target_column": transform.get("target_column"),
                        "output_name": transform.get("output_name"), "branch": transform["branch"],
                        "scope_id": transform["scope_id"], "statement_status": content.get("status"),
                        "statement_issues": content.get("issues", []), "metadata_snapshot": content.get("metadata_snapshot"),
                        "expression": transform["expression"], "sources": transform["sources"],
                        "aggregations": transform["aggregations"], "windows": transform["windows"],
                        "group_by": scope.get("group_by", []), "filters": scope.get("where"),
                        "having": scope.get("having"), "joins": scope.get("joins", []),
                        "distinct": scope.get("distinct", False),
                        "metadata": transform.get("target_column_metadata"),
                        "parser_issues": transform.get("issues", []),
                        "requires": ["business_definition", "unit", "owner_or_authoritative_support",
                                     "time_semantics", "additivity", "physical_mapping_review"]})
                if content.get("status") != "parsed":
                    issues.append({"block_id": bid, "kind": "etl_requires_review", "details": content.get("issues", [])})
            elif data["source_type"] == "prompt":
                if len(candidates) >= max_candidates:
                    raise ValueError("Candidate budget exceeded; narrow prompt exports")
                candidates.append({"kind": "user_statement", "status": "candidate", "evidence": [bid],
                                   "content": content, "requires": ["scope_and_authority_review", "cross_source_reconciliation"]})
            else:
                # Raw documents/catalog/code stay in original evidence; ledger accounts for every block.
                entry["review_input"] = "Read original evidence content before deciding assertions"
    write_new(output / "source-inventory.json", list(sources.values()))
    write_new(output / "candidates.json", candidates)
    write_new(output / "merge-ledger.json", ledger)
    write_new(output / "issues.json", issues)
    return {"sources": len(sources), "blocks": len(ledger), "candidates": len(candidates),
            "issues": len(issues), "semantic_review": "pending", "ontology_generated": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-blocks", type=int, default=5000)
    parser.add_argument("--max-candidates", type=int, default=10000)
    parser.add_argument("--max-bytes", type=int, default=50_000_000)
    args = parser.parse_args()
    try:
        result = prepare(args.evidence, args.output, args.max_blocks, args.max_candidates, args.max_bytes)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result["issues"] else 0
    except (ValueError, KeyError, OSError) as exc:
        print("Review preparation failed: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
