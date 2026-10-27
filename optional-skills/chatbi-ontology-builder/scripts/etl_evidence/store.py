"""Local, resumable ETL evidence index and bounded ChatBI evidence export.

No warehouse connections, SQL execution or ontology publication.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from datetime import datetime, timezone

import sqlglot

from .sql_evidence import PARSER_VERSION, analyze_sql, digest, split_sql, table_key

log = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (source_id TEXT PRIMARY KEY, root TEXT NOT NULL, engine TEXT NOT NULL,
 scan_state TEXT NOT NULL DEFAULT 'incomplete');
CREATE TABLE IF NOT EXISTS files (
 source_id TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL, parser_key TEXT NOT NULL,
 status TEXT NOT NULL, issues TEXT NOT NULL, PRIMARY KEY(source_id,path));
CREATE TABLE IF NOT EXISTS statements (
 statement_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, path TEXT NOT NULL,
 ordinal INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS statement_file ON statements(source_id,path);
CREATE TABLE IF NOT EXISTS table_refs (
 statement_id TEXT NOT NULL, table_key TEXT NOT NULL, role TEXT NOT NULL,
 PRIMARY KEY(statement_id,table_key,role));
CREATE INDEX IF NOT EXISTS table_lookup ON table_refs(table_key,role);
"""


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if "metadata_platform" not in {r[1] for r in conn.execute("PRAGMA table_info(settings)")}:
        conn.execute("ALTER TABLE settings ADD COLUMN metadata_platform TEXT")
    if "scan_origin" not in {r[1] for r in conn.execute("PRAGMA table_info(settings)")}:
        conn.execute("ALTER TABLE settings ADD COLUMN scan_origin TEXT NOT NULL DEFAULT '{}'")
    return conn


def _remove(conn, source_id, path):
    conn.execute("DELETE FROM table_refs WHERE statement_id IN "
                 "(SELECT statement_id FROM statements WHERE source_id=? AND path=?)", (source_id, path))
    conn.execute("DELETE FROM statements WHERE source_id=? AND path=?", (source_id, path))
    conn.execute("DELETE FROM files WHERE source_id=? AND path=?", (source_id, path))


def collect(root, database, source_id, dialect="teradata", engine="unknown", max_file_bytes=5_000_000,
            max_statement_bytes=500_000, metadata_path=None, origin=None):
    root = Path(root).expanduser().resolve(strict=True)
    origin = origin or {"read_mode": "worktree_file_snapshot", "issues": []}
    root_identity = origin.get("repository", str(root))
    provenance = {k: origin[k] for k in ("read_mode", "repository", "commit", "patterns") if k in origin}
    if not root.is_dir() or not source_id:
        raise ValueError("A directory and nonempty source_id are required")
    if max_file_bytes < 1 or max_statement_bytes < 1:
        raise ValueError("File and statement budgets must be positive")
    parser_dir = Path(__file__).resolve().parent
    from .metadata import PlatformMetadata
    metadata = PlatformMetadata.load(metadata_path) if metadata_path else None
    if metadata and (metadata.data["source_id"] != source_id or metadata.data["engine"] != engine):
        raise ValueError("Metadata source_id/engine does not match this evidence source")
    implementation = digest_bytes((parser_dir / "sql_evidence.py").read_bytes() +
                                  (parser_dir / "distributed_rewriter.py").read_bytes() +
                                  (parser_dir / "schema_binding.py").read_bytes() +
                                  (parser_dir / "metadata.py").read_bytes() +
                                  Path(__file__).read_bytes())
    key = digest(json.dumps([PARSER_VERSION, implementation, sqlglot.__version__, dialect, engine,
                             max_file_bytes, max_statement_bytes, metadata.data["fingerprint"] if metadata else None]))
    stats = {"processed_files": 0, "cached_files": 0, "deleted_files": 0, "review_files": 0}
    seen = set()
    conn = connect(database)
    try:
        old = conn.execute("SELECT * FROM settings WHERE source_id=?", (source_id,)).fetchone()
        if old and (old["root"] != root_identity or old["engine"] != engine):
            raise ValueError("source_id is already bound to another root/engine")
        platform_id = str(metadata.data["platform"]["plat_id"]) if metadata else None
        if old and old["metadata_platform"] and platform_id and old["metadata_platform"] != platform_id:
            raise ValueError("source_id is already bound to another metadata platform")
        conn.execute("INSERT OR IGNORE INTO settings(source_id,root,engine) VALUES (?,?,?)", (source_id, root_identity, engine))
        conn.execute("UPDATE settings SET scan_state='incomplete' WHERE source_id=?", (source_id,))
        conn.execute("UPDATE settings SET scan_origin=? WHERE source_id=?",
                     (json.dumps({k: v for k, v in origin.items() if k not in ("files", "file_hashes")}), source_id))
        if platform_id:
            conn.execute("UPDATE settings SET metadata_platform=? WHERE source_id=?", (platform_id, source_id))
        conn.commit()

        def on_walk_error(error):
            # Abort, rather than retiring files from an incompletely traversed directory.
            raise error

        for directory, dirs, names in os.walk(root, followlinks=False, onerror=on_walk_error):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not Path(directory, d).is_symlink())
            for name in sorted(names):
                path = Path(directory, name)
                if path.suffix.lower() not in (".sql", ".ddl") or path.is_symlink():
                    continue
                relative = path.relative_to(root).as_posix()
                if origin["read_mode"] == "git_commit" and relative not in origin["files"]:
                    continue
                seen.add(relative)
                with path.open("rb") as stream:
                    raw = stream.read(max_file_bytes + 1)
                checksum = digest_bytes(raw)
                if origin["read_mode"] == "git_commit" and checksum != origin["file_hashes"][relative]:
                    raise ValueError("Materialized Git snapshot changed before indexing")
                cached = conn.execute("SELECT sha256,parser_key,status FROM files WHERE source_id=? AND path=?",
                                      (source_id, relative)).fetchone()
                if cached and cached["sha256"] == checksum and cached["parser_key"] == key:
                    # Unchanged blobs can reuse AST results at a new commit, while refreshing provenance.
                    with conn:
                        for row in conn.execute("SELECT statement_id,payload FROM statements WHERE source_id=? AND path=?",
                                                (source_id, relative)).fetchall():
                            record = json.loads(row["payload"])
                            for field in ("read_mode", "repository", "commit", "patterns", "git_blob_id"):
                                record.pop(field, None)
                            record.update(provenance)
                            if relative in origin.get("files", {}):
                                record["git_blob_id"] = origin["files"][relative]
                            conn.execute("UPDATE statements SET payload=? WHERE statement_id=?",
                                         (json.dumps(record, ensure_ascii=False), row["statement_id"]))
                    stats["cached_files"] += 1
                    continue
                issues, records = [], []
                try:
                    if len(raw) > max_file_bytes:
                        raise ValueError("file_budget_exceeded")
                    text = raw.decode("utf-8-sig")
                    for ordinal, fragment in enumerate(split_sql(text), 1):
                        record = {"statement_id": digest(json.dumps([source_id, relative, checksum, key, ordinal])),
                                  "source_id": source_id, "engine": engine, "file_sha256": checksum,
                                  "parser_version": PARSER_VERSION, "sqlglot_version": sqlglot.__version__,
                                  "parser_key": key,
                                  "dialect": dialect, "path": relative, "ordinal": ordinal,
                                  "captured_at": datetime.now(timezone.utc).isoformat(),
                                  "read_mode": "worktree_file_snapshot", **fragment}
                        record.update(provenance)
                        if relative in origin.get("files", {}):
                            record["git_blob_id"] = origin["files"][relative]
                        if fragment["lexical_issue"]:
                            parsed = {"status": "error", "issues": [fragment["lexical_issue"]]}
                        elif len(fragment["sql"].encode("utf-8")) > max_statement_bytes:
                            parsed = {"status": "unsupported", "issues": ["statement_budget_exceeded"]}
                        else:
                            parsed = analyze_sql(fragment["sql"], dialect, metadata=metadata)
                        record.update(parsed)
                        records.append(record)
                    if not records:
                        issues.append("no_sql_statements")
                    issues.extend(f"statement {r['ordinal']}: " + ", ".join(r["issues"])
                                  for r in records if r["status"] != "parsed")
                except (UnicodeError, ValueError) as exc:
                    issues.append(str(exc))
                file_status = "partial" if issues or any(r["status"] != "parsed" for r in records) else "parsed"
                with conn:
                    _remove(conn, source_id, relative)
                    conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?)",
                                 (source_id, relative, checksum, key, file_status, json.dumps(issues)))
                    for record in records:
                        sid = record["statement_id"]
                        conn.execute("INSERT INTO statements VALUES (?,?,?,?,?,?)",
                                     (sid, source_id, relative, record["ordinal"], record["status"],
                                      json.dumps(record, ensure_ascii=False)))
                        for table in record.get("tables", []):
                            conn.execute("INSERT OR IGNORE INTO table_refs VALUES (?,?,?)",
                                         (sid, table_key(table), table["role"]))
                stats["processed_files"] += 1
                stats["review_files"] += file_status != "parsed"
                if stats["processed_files"] % 100 == 0:
                    log.info("证据抽取: %s", stats)
        for row in conn.execute("SELECT path FROM files WHERE source_id=?", (source_id,)).fetchall():
            if row["path"] not in seen:
                with conn:
                    _remove(conn, source_id, row["path"])
                stats["deleted_files"] += 1
        stats["statement_statuses"] = dict(conn.execute(
            "SELECT status,COUNT(*) FROM statements WHERE source_id=? GROUP BY status", (source_id,)).fetchall())
        stats["file_statuses"] = dict(conn.execute(
            "SELECT status,COUNT(*) FROM files WHERE source_id=? GROUP BY status", (source_id,)).fetchall())
        stats["origin_issues"] = origin.get("issues", [])
        stats["complete"] = (bool(stats["file_statuses"]) and not stats["origin_issues"] and
                             not any(s != "parsed" for s in stats["file_statuses"]))
        conn.execute("UPDATE settings SET scan_state='finished' WHERE source_id=?", (source_id,))
        conn.commit()
        return stats
    finally:
        conn.close()


def digest_bytes(raw):
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def query(database, source_id, table, limit=20, role=None):
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be 1..1000")
    tables = [table] if isinstance(table, str) else list(table)
    if not tables or len(tables) > 100:
        raise ValueError("Select 1..100 qualified table names")
    tables = sorted({t.upper() for t in tables})
    conn = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        where = "s.source_id=? AND r.table_key IN (" + ",".join(["?"] * len(tables)) + ")"
        args = [source_id, *tables]
        if role:
            where += " AND r.role=?"
            args.append(role)
        total = conn.execute("SELECT COUNT(DISTINCT s.statement_id) FROM statements s "
                             "JOIN table_refs r USING(statement_id) WHERE " + where, args).fetchone()[0]
        rows = conn.execute("SELECT DISTINCT s.statement_id,s.payload FROM statements s "
                            "JOIN table_refs r USING(statement_id) WHERE " + where +
                            " ORDER BY s.path,s.ordinal LIMIT ?", [*args, limit]).fetchall()
        file_issues = [dict(row) for row in conn.execute(
            "SELECT path,status,issues FROM files WHERE source_id=? AND status!='parsed' LIMIT 100", (source_id,))]
        incomplete = conn.execute("SELECT COUNT(*) FROM files WHERE source_id=? AND status!='parsed'",
                                  (source_id,)).fetchone()[0]
        setting = conn.execute("SELECT * FROM settings WHERE source_id=?", (source_id,)).fetchone()
        if not setting:
            raise ValueError("Unknown source_id")
        return {"table": tables[0] if len(tables) == 1 else tables, "total_matches": total, "returned": len(rows),
                "scan_origin": json.loads(setting["scan_origin"]),
                "source_root": setting["root"], "engine": setting["engine"],
                "limit_reached": total > len(rows), "source_incomplete_files": incomplete,
                "scan_finished": setting["scan_state"] == "finished",
                "source_issues_sample": file_issues,
                "statements": [json.loads(r["payload"]) for r in rows]}
    finally:
        conn.close()


def export(database, source_id, table, output, limit=20, max_chars=200_000, role=None):
    """The installed chatbi skill's evidence envelope; no skill dependency required."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    result = query(database, source_id, table, limit, role)
    source_root, engine = result.pop("source_root"), result.pop("engine")
    blocks, omitted, used = [], [], 0
    for record in result.pop("statements"):
        locator = {key: record[key] for key in ("path", "line_start", "line_end", "file_sha256", "ordinal")}
        locator.update(statement_id=record["statement_id"], read_mode=record["read_mode"])
        locator.update({k: record[k] for k in ("repository", "commit", "git_blob_id") if k in record})
        content = redact_values(record)
        content["redaction_applied"] = content != record
        serialized = json.dumps([locator, content], ensure_ascii=False, sort_keys=True)
        if used + len(serialized) > max_chars:
            omitted.append(record["statement_id"])
            continue
        used += len(serialized)
        blocks.append({"block_id": source_id + ":" + digest(serialized)[:20],
                       "block_type": "etl_sql_evidence", "locator": locator, "content": content})
    result["omitted_statement_ids"] = omitted
    complete = bool(blocks) and result["scan_finished"] and not (result["scan_origin"].get("issues") or omitted or result["limit_reached"] or
               result["source_incomplete_files"] or any(b["content"]["status"] != "parsed" for b in blocks))
    evidence = {"evidence_version": "1.0", "source_id": source_id, "source_type": "etl",
                "extracted_at": datetime.now(timezone.utc).isoformat(),
                "identity": {"index": str(Path(database).resolve()), "table": table,
                             "source_root": source_root, "engine": engine,
                             "read_mode": result["scan_origin"].get("read_mode", "worktree_file_snapshot"),
                             "metadata_snapshots": list({b["content"]["metadata_snapshot"]["fingerprint"]:
                                 b["content"]["metadata_snapshot"] for b in blocks
                                 if b["content"].get("metadata_snapshot")}.values()),
                             "physical_binding": "catalog_snapshot" if blocks and all(
                                 b["content"].get("physical_binding") == "catalog_snapshot" for b in blocks)
                                 else "unverified_or_partial"},
                "coverage": {"complete": complete, "issues": [] if complete else ["partial_or_unverified_coverage"],
                             **result}, "blocks": blocks}
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with Path(output).open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return evidence


def redact_values(value):
    # Same limited credential redaction as the skill; evidence remains access-controlled.
    if isinstance(value, str):
        value = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value)
        return re.sub(r'''(?ix)((?:["']?)(?:password|passwd|secret|token|api_key|access_key|secret_key)(?:["']?)\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;<>]+)''',
                      r'\1"[REDACTED]"', value)
    if isinstance(value, dict):
        return {k: redact_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_values(v) for v in value]
    return value
