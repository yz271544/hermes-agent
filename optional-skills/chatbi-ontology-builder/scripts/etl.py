#!/usr/bin/env python3
"""Incrementally index ETL SQL and export bounded ontology evidence, never execute ETL."""
import argparse
import json
import logging
from pathlib import Path
import sys

from etl_evidence.store import collect, query, export
from etl_evidence.metadata import capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan")
    scan.add_argument("repository", type=Path)
    scan.add_argument("--index", type=Path, required=True)
    scan.add_argument("--source-id", required=True)
    scan.add_argument("--engine", default="unknown")
    scan.add_argument("--dialect", default="teradata")
    scan.add_argument("--metadata", type=Path)
    scan.add_argument("--max-file-bytes", type=int, default=5_000_000)
    scan.add_argument("--max-statement-bytes", type=int, default=500_000)
    scan.add_argument("--revision", help="Read the specified committed Git tree, not working files")
    scan.add_argument("--include", action="append", help="Git path glob; repeat to select scoped paths")
    scan.add_argument("--max-files", type=int, default=50_000)
    metadata = sub.add_parser("metadata-snapshot")
    metadata.add_argument("--config", type=Path, required=True)
    metadata.add_argument("--index", type=Path, required=True)
    metadata.add_argument("--source-id", required=True)
    metadata.add_argument("--platform", required=True)
    metadata.add_argument("--output", type=Path, required=True)
    metadata.add_argument("--max-tables", type=int, default=1000)
    for name in ("query", "export"):
        item = sub.add_parser(name)
        item.add_argument("--index", type=Path, required=True)
        item.add_argument("--source-id", required=True)
        item.add_argument("--table", required=True, action="append", help="Repeat to export related tables in one source envelope")
        item.add_argument("--role", choices=("read", "write", "reference"))
        item.add_argument("--limit", type=int, default=20)
        if name == "export":
            item.add_argument("--output", type=Path, required=True)
            item.add_argument("--max-chars", type=int, default=200_000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "scan":
            origin = None
            root = args.repository
            if args.revision:
                from etl_evidence.git_snapshot import materialize
                root, origin = materialize(args.repository, args.revision,
                    args.index.parent / "git-snapshots", args.source_id,
                    args.include or ["*.sql", "*.ddl"], args.max_file_bytes, args.max_files)
            elif args.include:
                raise ValueError("--include currently requires --revision; otherwise select a directory")
            result = collect(root, args.index, args.source_id, args.dialect, args.engine,
                             args.max_file_bytes, args.max_statement_bytes, args.metadata, origin=origin)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["complete"] else 2
        if args.command == "metadata-snapshot":
            config = json.loads(args.config.read_text(encoding="utf-8"))
            result = capture(args.index, args.source_id, args.platform, args.output,
                             config=config, max_tables=args.max_tables)
            print(json.dumps({"platform": result["platform"], "tables": len(result["tables"]),
                  "columns": sum(len(t["columns"]) for t in result["tables"]),
                  "ddl_status": result["ddl_status"], "issues": result["issues"]}, ensure_ascii=False, indent=2))
            return 0 if not result["issues"] else 2
        if args.command == "query":
            result = query(args.index, args.source_id, args.table, args.limit, args.role)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        result = export(args.index, args.source_id, args.table, args.output,
                        args.limit, args.max_chars, args.role)
        print(json.dumps({"output": str(args.output), "blocks": len(result["blocks"]),
                          "coverage": result["coverage"]}, ensure_ascii=False, indent=2))
        return 0 if result["coverage"]["complete"] else 2
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        print("ETL step failed: " + str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Do not echo driver exceptions/configuration that can include endpoints/credentials.
        print("ETL step failed: " + type(exc).__name__ + "; inspect local inputs and configured access", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
