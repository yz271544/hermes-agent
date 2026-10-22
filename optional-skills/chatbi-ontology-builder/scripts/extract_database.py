#!/usr/bin/env python3
"""Read scoped Doris/GBase 8a metadata and optional bounded row evidence."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

from evidence import block, envelope, load_config, redact, redact_values, sha256, write_new

ENGINES = {"doris", "gbase8a"}


def ident(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid SQL identifier")
    return "`" + value.replace("`", "``") + "`"


def qualified(config: dict, table: str) -> str:
    parts = [config["database"], table]
    if config.get("catalog"):
        if config["engine"] != "doris":
            raise ValueError("GBase 8a catalog is not a Doris catalog")
        parts.insert(0, config["catalog"])
    return ".".join(map(ident, parts))


def validate_config(config: dict) -> None:
    if config.get("engine") not in ENGINES:
        raise ValueError("engine must be doris or gbase8a")
    for key in ("source_id", "host", "port", "database", "user", "password_env"):
        if not config.get(key):
            raise ValueError("Missing configuration: " + key)
    tables = config.get("tables")
    if not isinstance(tables, list) or not tables or len(tables) > 500 or len(set(tables)) != len(tables):
        raise ValueError("Provide 1..500 distinct explicit table names")
    for table in tables:
        qualified(config, table)
    if not 1 <= config.get("timeout_seconds", 15) <= 120:
        raise ValueError("timeout_seconds must be in 1..120")
    sample = config.get("sampling", {})
    if not isinstance(sample, dict) or type(sample.get("enabled", False)) is not bool:
        raise ValueError("sampling must be an object with a boolean enabled flag")
    if config.get("tls") and (not isinstance(config["tls"], dict) or not config["tls"].get("ca")):
        raise ValueError("TLS configuration requires a CA file for identity verification")
    if not 1 <= sample.get("max_rows", 20) <= 1000:
        raise ValueError("sampling.max_rows must be in 1..1000")
    if sample.get("enabled") and config["engine"] == "gbase8a" and sample.get("server_budget_enforced") is not True:
        raise ValueError("GBase sampling requires a DBA-enforced server query budget; LIMIT/read_timeout do not bound server work")
    if "password" in config:
        raise ValueError("Use password_env, not an inline password")


def sample_sql(config: dict, table: str, known_columns: set[str]) -> tuple[str, list, list[str]]:
    sampling = config.get("sampling", {})
    columns = sampling.get("columns", {}).get(table, [])
    if not isinstance(columns, list) or not columns or len(set(columns)) != len(columns) or not set(columns) <= known_columns:
        raise ValueError("Sampling needs distinct, explicitly allowed real columns for " + table)
    predicates, params = [], []
    for item in sampling.get("filters", {}).get(table, []):
        if item["column"] not in known_columns or item["op"] not in {"=", ">=", ">", "<=", "<"}:
            raise ValueError("Unsupported sampling filter")
        predicates.append(ident(item["column"]) + " " + item["op"] + " %s")
        params.append(item["value"])
    sql = "SELECT " + ", ".join(map(ident, columns)) + " FROM " + qualified(config, table)
    if predicates:
        sql += " WHERE " + " AND ".join(predicates)
    sql += " LIMIT " + str(sampling.get("max_rows", 20))
    return sql, params, columns


def profile_rows(rows: list[dict], columns: list[str]) -> dict:
    result = {}
    for column in columns:
        values = [row[column] for row in rows]
        nonnull = [value for value in values if value is not None]
        counts = Counter(str(value) for value in nonnull)
        result[column] = {"sample_rows": len(rows), "null_count": len(values) - len(nonnull),
                          "distinct_in_sample": len(counts), "max_frequency_in_sample": max(counts.values(), default=0),
                          "population_claim": False}
    return result


def extract(config: dict, conn, database_errors=()) -> dict:
    validate_config(config)
    blocks, issues = [], []
    sid = config["source_id"]
    timeout = config.get("timeout_seconds", 15)
    with conn.cursor() as cur:
        if config["engine"] == "doris":
            cur.execute("SET query_timeout = " + str(timeout))
            if config.get("catalog"):
                cur.execute("SWITCH " + ident(config["catalog"]))
        cur.execute("SELECT VERSION() AS version")
        version = cur.fetchone()["version"]
        for table in config["tables"]:
            # Metadata views vary by version. Preserve native fields instead of
            # inventing MySQL keys or dropping Doris/GBase-specific fields.
            cur.execute("SELECT * FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                        (config["database"], table))
            info = [{str(k).lower(): v for k, v in row.items()} for row in cur.fetchall()]
            if len(info) != 1:
                issues.append({"table": table, "status": "missing_or_not_visible"})
                continue
            cur.execute("SELECT * FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                        (config["database"], table))
            columns = [{str(k).lower(): v for k, v in row.items()} for row in cur.fetchall()]
            if not columns:
                issues.append({"table": table, "status": "no_visible_columns"})
                continue
            locator = {"catalog": config.get("catalog"), "database": config["database"], "table": table}
            safe_info = redact_values(info[0])
            safe_columns = redact_values(columns)
            blocks.append(block(sid, "table_metadata", locator, {"table": safe_info, "columns": safe_columns}))
            try:
                cur.execute("SHOW CREATE TABLE " + qualified(config, table))
                ddl = cur.fetchone()
                ddl = {k: redact(str(v)) for k, v in ddl.items()}
                blocks.append(block(sid, "ddl", locator, ddl))
            except Exception as exc:
                # SQL/driver errors only: do not swallow coding or network failures.
                if not isinstance(exc, database_errors):
                    raise
                issues.append({"table": table, "status": "ddl_unavailable", "error_code": exc.args[0]})
            if config.get("sampling", {}).get("enabled"):
                names = {column["column_name"] for column in columns}
                sql, params, selected = sample_sql(config, table, names)
                reveal = set(config["sampling"].get("expose_values", {}).get(table, []))
                if not reveal <= set(selected):
                    raise ValueError("expose_values must be a subset of selected columns")
                cur.execute(sql, params)
                rows = cur.fetchmany(config["sampling"].get("max_rows", 20))
                values = [{k: redact(str(v))[:1000] if v is not None else None
                           for k, v in row.items() if k in reveal} for row in rows] if reveal else []
                blocks.append(block(sid, "sample_profile", locator, {
                    "query_template": sql, "filter_values_saved": False,
                    "filter_sha256": sha256(json.dumps(params, default=str).encode()),
                    "selection": "bounded_prefix_not_random", "limit_reached": len(rows) == config["sampling"].get("max_rows", 20),
                    "profile": profile_rows(rows, selected), "exposed_values": values,
                    "sample_is_not_constraint": True,
                }))
    return envelope(sid, "database", {"engine": config["engine"], "server_version": version,
                    "engine_identity": "configured_not_independently_attested", "database": config["database"],
                    "catalog": config.get("catalog")}, blocks,
                    {"requested_tables": config["tables"], "issues": issues, "complete": not issues,
                     "constraints": "native_column_metadata_and_ddl_only; no FK absence claim",
                     "server_budget": "doris_query_timeout" if config["engine"] == "doris" else "DBA-managed (verify separately)"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        validate_config(config)
        if args.output.exists():
            raise ValueError("Output already exists")
        password = os.environ.get(config["password_env"])
        if password is None:
            raise ValueError("Missing credential environment variable")
        import pymysql
        ssl = config.get("tls", {})
        options = {"ssl_ca": ssl["ca"], "ssl_verify_cert": True, "ssl_verify_identity": True} if ssl.get("ca") else {}
        with pymysql.connect(host=config["host"], port=int(config["port"]), user=config["user"], password=password,
                             database=config["database"], charset="utf8mb4", autocommit=True,
                             connect_timeout=config.get("timeout_seconds", 15), read_timeout=config.get("timeout_seconds", 15),
                             write_timeout=config.get("timeout_seconds", 15), local_infile=False,
                             cursorclass=pymysql.cursors.DictCursor, **options) as conn:
            result = extract(config, conn, database_errors=(pymysql.MySQLError,))
        write_new(args.output, result)
        print(json.dumps({"output": str(args.output), "complete": result["coverage"]["complete"]}))
        return 0 if result["coverage"]["complete"] else 2
    except (ValueError, OSError, ImportError) as exc:
        print(redact(str(exc)), file=sys.stderr)
        return 1
    except Exception as exc:
        # Drivers may echo credentials or SQL values. Report class, not raw text.
        print("Database extraction failed: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
