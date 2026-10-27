"""Scoped, read-only snapshots of the existing MySQL metadata catalog.

Never collapse cross-platform tables or infer column order from column_id.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from .connection import get_connection


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class PlatformMetadata:
    def __init__(self, data):
        self.data = data
        self.identity = {k: data[k] for k in ("source_id", "engine", "platform", "captured_at", "fingerprint")}
        expected = fingerprint({k: v for k, v in data.items() if k not in ("captured_at", "fingerprint")})
        if data["fingerprint"] != expected:
            raise ValueError("Metadata snapshot fingerprint mismatch")
        self.by_name = {}
        for table in data["tables"]:
            if str(table["plat_id"]) != str(data["platform"]["plat_id"]):
                raise ValueError("Snapshot contains another platform")
            if any((c.get("plat_id") is not None and str(c["plat_id"]) != str(table["plat_id"])) or
                   (c.get("table_id") is not None and str(c["table_id"]) != str(table["table_id"]))
                   for c in table["columns"]):
                raise ValueError("Snapshot column identity does not match its table/platform")
            self.by_name.setdefault(table["table_name"].upper(), []).append(table)

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def resolve(self, identity):
        matches = [t for t in self.by_name.get(identity["table"].upper(), [])
                   if all(not identity.get(sql) or str(t.get(meta) or "").upper() == identity[sql].upper()
                          for sql, meta in (("database", "database_name"), ("catalog", "catalog_name")))]
        if len(matches) != 1:
            return None, "metadata_table_ambiguous" if matches else "metadata_table_not_found"
        return matches[0], None

    @staticmethod
    def ordered_columns(table):
        columns = table["columns"]
        try:
            ordinals = [int(c["ordinal"]) for c in columns]
        except (ValueError, TypeError, KeyError):
            return None
        # The catalog's ordinal is 1-based. Gaps may indicate incomplete extraction.
        if not columns or sorted(ordinals) != list(range(1, len(columns) + 1)):
            return None
        if len({c["column_name"].upper() for c in columns}) != len(columns):
            return None
        return sorted(columns, key=lambda c: int(c["ordinal"]))

    @staticmethod
    def column(table, name):
        matches = [c for c in table["columns"] if c["column_name"].upper() == name.upper()]
        return matches[0] if len(matches) == 1 else None


def make_snapshot(source_id, engine, platform, tables, requested, issues=None):
    data = {"snapshot_version": "1", "source_id": source_id, "engine": engine,
            "platform": platform, "metadata_origin": "mysql_metadata_catalog",
            "ddl_status": "native_ddl_not_collected", "requested_tables": sorted(requested),
            "tables": sorted(tables, key=lambda t: (str(t["database_name"]), t["table_name"], str(t["table_id"]))),
            "issues": sorted(issues or [])}
    # Canonical ordering makes identical catalog content reuse the binding cache.
    for table in data["tables"]:
        table["columns"] = sorted(table["columns"], key=lambda c: str(c["column_id"]))
    data["fingerprint"] = fingerprint(data)
    data["captured_at"] = datetime.now(timezone.utc).isoformat()
    return data


def capture(index, source_id, platform_name, output, config=None, conn=None, max_tables=1000):
    if Path(output).exists():
        raise FileExistsError(output)
    with closing(sqlite3.connect(Path(index).resolve().as_uri() + "?mode=ro", uri=True)) as local:
        local.execute("BEGIN")
        setting = local.execute("SELECT engine FROM settings WHERE source_id=?", (source_id,)).fetchone()
        if not setting:
            raise ValueError("Unknown evidence source_id; run evidence-scan first")
        requested = [r[0] for r in local.execute(
            "SELECT DISTINCT r.table_key FROM table_refs r JOIN statements s USING(statement_id) "
            "WHERE s.source_id=? ORDER BY r.table_key", (source_id,))]
    if not requested:
        raise ValueError("No referenced tables in source")
    if max_tables < 1 or len(requested) > max_tables:
        raise ValueError("Requested metadata exceeds max_tables; narrow the source or explicitly increase the budget")
    by_name = {}
    for key in requested:
        parts = key.split(".")
        by_name.setdefault(parts[-1].upper(), set()).add(parts[-2].upper() if len(parts) >= 2 else None)
    own = conn is None
    conn = conn or get_connection(config)
    tables, issues = [], []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT plat_id,name FROM audit_source_plat WHERE name=%s", (platform_name,))
            platforms = cur.fetchall()
            if len(platforms) != 1:
                raise ValueError("Platform must match exactly one audit_source_plat.name")
            platform = platforms[0]
            names = sorted({variant for key in requested for variant in (key.split(".")[-1].lower(), key.split(".")[-1].upper())})
            for offset in range(0, len(names), 100):
                batch = names[offset:offset + 100]
                cur.execute("SELECT table_id,plat_id,plat_name,database_name,table_name,catalog_name,schema_name,"
                            "table_desc,update_time FROM tb_meta_tables WHERE plat_id=%s AND table_name IN (" +
                            ",".join(["%s"] * len(batch)) + ")", [platform["plat_id"], *batch])
                for row in cur.fetchall():
                    # Keep every candidate catalog/schema for each SQL name, never take the first.
                    databases = by_name.get(row["table_name"].upper(), set())
                    if None not in databases and (row["database_name"] or "").upper() not in databases:
                        continue
                    row["update_time"] = str(row["update_time"]) if row["update_time"] else None
                    row["columns"] = []
                    tables.append(row)
            # Case-insensitive MySQL catalogs can match both lower/upper request batches.
            tables = list({(str(t["plat_id"]), str(t["table_id"])): t for t in tables}.values())
            for offset in range(0, len(tables), 100):
                batch = tables[offset:offset + 100]
                by_id = {str(t["table_id"]): t for t in batch}
                cur.execute("SELECT table_id,plat_id,column_id,column_name,ordinal,column_origin_type,"
                            "column_edm_type,column_desc,is_primary_key,is_nullable,max_length,precisions,scale "
                            "FROM tb_meta_columns WHERE plat_id=%s AND table_id IN (" +
                            ",".join(["%s"] * len(batch)) + ")", [platform["plat_id"], *[t["table_id"] for t in batch]])
                for column in cur.fetchall():
                    if str(column["plat_id"]) != str(platform["plat_id"]):
                        raise ValueError("Catalog returned a column from another platform")
                    by_id[str(column["table_id"])]["columns"].append(column)
        data = make_snapshot(source_id, setting[0], platform, tables, requested)
        snapshot = PlatformMetadata(data)
        for key in requested:
            parts = key.split(".")
            table, issue = snapshot.resolve({"table": parts[-1], "database": parts[-2] if len(parts) >= 2 else None,
                                             "catalog": parts[-3] if len(parts) >= 3 else None})
            if issue:
                issues.append(key + ":" + issue)
            elif snapshot.ordered_columns(table) is None:
                issues.append(key + ":invalid_or_incomplete_ordinals")
        data = make_snapshot(source_id, setting[0], platform, tables, requested, issues)
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        with Path(output).open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, default=str)
            stream.write("\n")
        return data
    finally:
        if own:
            conn.close()
