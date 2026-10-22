#!/usr/bin/env python3
"""Read committed, scoped Git files and extract static SQL evidence, never run code."""
from __future__ import annotations

import argparse
import fnmatch
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

from evidence import block, envelope, redact, sha256, write_new

DEFAULT_PATTERNS = ["*.sql", "*.ddl", "*.java", "*.py", "*.go", "*.rs", "*.xml", "*.yml", "*.yaml"]
SKIP_PARTS = {".git", "node_modules", "vendor", "target", "dist", ".venv", "venv", "__pycache__"}


def git(repo: Path, *args: str, binary=False):
    result = subprocess.run(["git", "-C", str(repo), "--no-pager", *args], capture_output=True,
                            timeout=60, check=True)
    return result.stdout if binary else result.stdout.decode("utf-8").strip()


def sql_evidence(text: str, dialect="mysql") -> dict:
    import sqlglot
    from sqlglot import exp
    if any(marker in text for marker in ("{{", "{%", "${", "#{")):
        return {"parse_status": "requires_review", "reason": "dynamic_sql_template", "statements": []}
    try:
        trees = sqlglot.parse(text, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
        statements = []
        for tree in trees:
            if tree is None:
                continue
            if isinstance(tree, exp.Command):
                return {"parse_status": "requires_review", "reason": "unsupported_statement", "statements": []}
            ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
            tables = sorted({table.sql(dialect=dialect) for table in tree.find_all(exp.Table)
                             if table.db or table.catalog or table.name not in ctes})
            statements.append({"statement_type": tree.key, "tables": tables,
                               "columns": sorted({column.sql(dialect=dialect) for column in tree.find_all(exp.Column)}),
                               "joins": [join.sql(dialect=dialect) for join in tree.find_all(exp.Join)],
                               "aggregations": [agg.sql(dialect=dialect) for agg in tree.find_all(exp.AggFunc)],
                               "filters": [where.sql(dialect=dialect) for where in tree.find_all(exp.Where)]})
        return {"parse_status": "parsed", "statements": statements, "physical_binding": "unverified",
                "scope_resolution": "syntactic_only; CTE shadowing, dynamic SQL and column lineage require review"}
    except sqlglot.errors.SqlglotError:
        return {"parse_status": "requires_review", "reason": "template_or_unsupported_dialect", "statements": []}


def extract(repo: Path, revision: str, patterns: list[str], sid: str, max_bytes=5_000_000, dialect="mysql") -> dict:
    if revision.startswith("-"):
        raise ValueError("Invalid revision")
    commit = git(repo, "rev-parse", "--verify", revision + "^{commit}")
    filenames = git(repo, "ls-tree", "-r", "--name-only", "-z", commit, binary=True).decode().split("\0")
    blocks, files, issues = [], [], []
    for name in filter(None, filenames):
        parts = PurePosixPath(name).parts
        if not any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
            continue
        if set(parts) & SKIP_PARTS or any(part.startswith(".env") for part in parts) or re.search(r"(?i)(secret|credential|private.?key|\.pem$|\.key$)", name):
            issues.append({"file": name, "status": "excluded_sensitive_or_generated"})
            continue
        # Check mode before reading, so symlinks/submodules are never followed.
        mode = git(repo, "ls-tree", commit, "--", name).split()[0]
        if mode not in {"100644", "100755"}:
            issues.append({"file": name, "status": "unsupported_git_object"})
            continue
        if int(git(repo, "cat-file", "-s", commit + ":" + name)) > max_bytes:
            issues.append({"file": name, "status": "file_budget_exceeded"})
            continue
        raw = git(repo, "show", commit + ":" + name, binary=True)
        try:
            text = redact(raw.decode("utf-8-sig"))
        except UnicodeError:
            issues.append({"file": name, "status": "non_utf8_requires_conversion"})
            continue
        files.append({"path": name, "sha256": sha256(raw)})
        lines = text.splitlines()
        for start in range(0, len(lines), 80):
            blocks.append(block(sid, "code_text", {"commit": commit, "path": name, "line_start": start + 1,
                                                    "line_end": min(start + 80, len(lines))}, "\n".join(lines[start:start + 80])))
        if Path(name).suffix.lower() in {".sql", ".ddl"}:
            parsed = sql_evidence(text, dialect)
            blocks.append(block(sid, "sql_structure", {"commit": commit, "path": name,
                                                        "line_start": 1, "line_end": len(lines)}, parsed))
            if parsed["parse_status"] != "parsed":
                issues.append({"file": name, "status": parsed["parse_status"]})
    return envelope(sid, "git", {"repository": str(repo.resolve()), "commit": commit,
                    "worktree_changes_included": False, "files": files}, blocks,
                    {"patterns": patterns, "complete": bool(files) and not issues, "issues": issues,
                     "semantic_extraction": "SQL AST + located source blocks; ORM/XML/ETL semantics require LLM review"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--include", action="append", help="Repeat scoped globs; use quotes")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--dialect", default="mysql", help="sqlglot dialect; mysql is a syntax fallback, not GBase/Doris attestation")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("Output already exists")
        result = extract(args.repository, args.revision, args.include or DEFAULT_PATTERNS, args.source_id, dialect=args.dialect)
        write_new(args.output, result)
        print(json.dumps({"output": str(args.output), "commit": result["identity"]["commit"], "files": len(result["identity"]["files"])}))
        return 0 if result["coverage"]["complete"] else 2
    except (ValueError, OSError, ImportError, subprocess.SubprocessError) as exc:
        print("Repository extraction failed: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
