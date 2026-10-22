#!/usr/bin/env python3
"""Discover pg-ripple or load a validated bundle into an explicitly empty graph.

Does not create tenants, grant roles, clear graphs, infer, execute R2RML, or replace releases.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

from evidence import load_config, sha256
from validate_bundle import TTL_FILES

TARGET = "https://arcp.sxmcc.com/tenant/chatbi"


def discover(cur) -> dict:
    cur.execute("SELECT current_setting('server_version_num')::int")
    version = cur.fetchone()[0]
    cur.execute("SELECT extversion FROM pg_extension WHERE extname='pg_ripple'")
    extension = cur.fetchone()
    if not 180000 <= version < 190000 or not extension:
        raise ValueError("Expected PostgreSQL 18 with pg_ripple installed")
    cur.execute("""SELECT p.proname, pg_catalog.oidvectortypes(p.proargtypes),
                          pg_catalog.pg_get_function_result(p.oid)
                   FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                   WHERE n.nspname='pg_ripple' ORDER BY p.proname, p.oid""")
    return {"postgres_version_num": version, "extension_version": extension[0],
            "functions": [{"name": name, "argument_types": args, "result": result}
                          for name, args, result in cur.fetchall()]}


def scalar(value):
    if isinstance(value, dict):
        return scalar(value["value"])
    return int(value)


def count_graph(cur, graph: str) -> int:
    query = "SELECT (COUNT(*) AS ?c) WHERE { GRAPH <" + graph + "> { ?s ?p ?o } }"
    cur.execute("SELECT * FROM pg_ripple.sparql(%s)", (query,))
    rows = cur.fetchall()
    if len(rows) != 1 or len(rows[0]) != 1:
        raise ValueError("Unsupported SPARQL count result envelope")
    result = rows[0][0]
    if isinstance(result, str):
        result = json.loads(result)
    return scalar(result["c"])


def validate_graph_iri(graph: str) -> None:
    parsed = urlparse(graph)
    if parsed.scheme != "https" or parsed.netloc != "arcp.sxmcc.com" or any(c in graph for c in '<>"{}|\\^` \n\r\t'):
        raise ValueError("Invalid target graph IRI")
    if graph != TARGET and not graph.startswith(TARGET + "/staging/"):
        raise ValueError("Use the ChatBI tenant graph or its explicitly provisioned staging graph")


def load_empty(conn, root: Path, graph: str, review: dict) -> dict:
    from validate_rdf import check
    validate_graph_iri(graph)
    report = check(root, shacl=True)
    if not report["ok"]:
        raise ValueError("Bundle failed RDF/SHACL validation")
    manifest_hash = sha256((root / "manifest.json").read_bytes())
    if (review.get("manifest_sha256") != manifest_hash or not review.get("authorization_reference")
            or any(review.get(key) is not True for key in ("approved", "cq_passed", "source_references_refreshed"))):
        raise ValueError("Need a current, bundle-bound source/CQ review and authorization record")
    if not report["rdf"]["shacl"]["node_shapes"]:
        raise ValueError("Release requires at least one reviewed SHACL NodeShape")
    # Read once and hash again, so the exact bytes sent were the validated artifacts.
    manifest = load_config(root / "manifest.json")
    entries = manifest["artifacts"]
    if isinstance(entries, list):
        entries = {item["path"]: item for item in entries}
    payloads = {name: (root / name).read_bytes() for name in TTL_FILES}
    if any(sha256(data) != entries[name]["sha256"] for name, data in payloads.items()):
        raise ValueError("Artifacts changed after validation")
    if sha256((root / "manifest.json").read_bytes()) != manifest_hash:
        raise ValueError("Manifest changed after validation")
    expected = sum(entries[name]["triples"] for name in TTL_FILES)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '60s'")
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (graph,))
            capabilities = discover(cur)
            signatures = {(f["name"], f["argument_types"]) for f in capabilities["functions"]}
            if ("load_turtle_into_graph", "text, text") not in signatures or ("sparql", "text") not in signatures:
                raise ValueError("Installed public loader/query signatures are unsupported; use a verified MCP adapter")
            if count_graph(cur, graph) != 0:
                raise ValueError("Graph is not empty; use a reviewed backup/promotion/rollback procedure, not this loader")
            for name in TTL_FILES:
                cur.execute("SELECT pg_ripple.load_turtle_into_graph(%s, %s)", (payloads[name].decode(), graph))
                cur.fetchone()
            actual = count_graph(cur, graph)
            if actual != expected:
                raise ValueError("Loaded graph count differs from explicit bundle triples; transaction aborted")
    return {"status": "staged" if graph != TARGET else "published", "target_graph": graph,
            "manifest_sha256": manifest_hash, "explicit_triples": actual,
            "extension_version": capabilities["extension_version"],
            "inference_executed": False, "r2rml_executed": False,
            "online_cq": "run the reviewed application SPARQL queries after load",
            "rollback": "transaction rollback on failure; post-commit graph retirement requires a verified authorized procedure"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["discover", "load-empty"])
    parser.add_argument("--dsn-env", default="CHATBI_PG_DSN")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--graph")
    parser.add_argument("--review-record", type=Path)
    parser.add_argument("--execute", action="store_true", help="Requires user authorization for this exact target")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("Output already exists")
        if args.action == "load-empty" and not all([args.execute, args.bundle, args.graph, args.review_record]):
            raise ValueError("load-empty requires --execute, --bundle, --graph, --review-record")
        import psycopg
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Reserve a writable receipt before any external operation. An interrupted
        # or ambiguous commit leaves an explicit pending record for reconciliation.
        with args.output.open("x", encoding="utf-8") as receipt:
            json.dump({"status": "in_progress_or_interrupted", "action": args.action, "graph": args.graph}, receipt)
            receipt.flush()
            with psycopg.connect(os.environ[args.dsn_env], connect_timeout=15) as conn:
                if args.action == "discover":
                    with conn.cursor() as cur:
                        cur.execute("SET TRANSACTION READ ONLY")
                        cur.execute("SET LOCAL statement_timeout = '15s'")
                        result = discover(cur)
                else:
                    result = load_empty(conn, args.bundle, args.graph, load_config(args.review_record))
            receipt.seek(0)
            json.dump(result, receipt, ensure_ascii=False, indent=2)
            receipt.truncate()
        print(json.dumps({"output": str(args.output), "status": result.get("status", "discovered")}))
        return 0
    except (ValueError, KeyError, OSError, ImportError) as exc:
        print("pg-ripple operation failed: " + type(exc).__name__ + ". Check configuration, review record and installed capabilities.", file=sys.stderr)
        return 1
    except Exception as exc:
        print("pg-ripple operation failed: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
