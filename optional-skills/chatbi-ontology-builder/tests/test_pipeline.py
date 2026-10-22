"""Behavior tests for scoped extraction, evidence conservation and bundle gates.

Run: python3 -m unittest discover -s tests -v
Optional dependencies enable additional real parser/assembly tests; no live servers used.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from evidence import block, envelope, redact_values, sha256, write_new
from extract_database import extract, sample_sql, validate_config
from extract_documents import parse
from pg_ripple import validate_graph_iri, scalar
from validate_bundle import TTL_FILES, validate

RDF = importlib.util.find_spec("rdflib") is not None
SHACL = importlib.util.find_spec("pyshacl") is not None
XLSX = importlib.util.find_spec("openpyxl") is not None
SQL = importlib.util.find_spec("sqlglot") is not None
BASE = "https://arcp.sxmcc.com/zb_data/chatbi/"


def config(engine="doris"):
    return {"source_id": "warehouse", "engine": engine, "host": "db", "port": 9030,
            "database": "biz", "user": "reader", "password_env": "TEST_DB_PASSWORD",
            "tables": ["metrics"], "sampling": {"enabled": False}}


class ExtractionTests(unittest.TestCase):
    def test_query_values_are_bound_and_identifiers_escaped(self):
        cfg = config()
        cfg["sampling"] = {"columns": {"metrics": ["a`b"]},
                           "filters": {"metrics": [{"column": "a`b", "op": "=", "value": "'; DROP TABLE x;--"}]},
                           "max_rows": 7}
        sql, params, columns = sample_sql(cfg, "metrics", {"a`b"})
        self.assertIn("`a``b`", sql)
        self.assertIn("LIMIT 7", sql)
        self.assertNotIn("DROP", sql)
        self.assertEqual(params, ["'; DROP TABLE x;--"])
        self.assertEqual(columns, ["a`b"])
        cfg["sampling"]["columns"]["metrics"] = ["absent"]
        with self.assertRaises(ValueError):
            sample_sql(cfg, "metrics", {"a`b"})

    def test_gbase_sampling_requires_external_budget(self):
        cfg = config("gbase8a")
        validate_config(cfg)
        cfg["sampling"]["enabled"] = True
        with self.assertRaises(ValueError):
            validate_config(cfg)
        cfg["sampling"]["server_budget_enforced"] = True
        validate_config(cfg)

    def test_native_metadata_and_non_value_profile(self):
        cfg = config()
        cfg["sampling"] = {"enabled": True, "columns": {"metrics": ["metric_value"]}, "max_rows": 3}
        cur = Mock()
        cur.__enter__ = Mock(return_value=cur)
        cur.__exit__ = Mock(return_value=False)
        cur.fetchone.side_effect = [{"version": "test"}, {"Create Table": "CREATE TABLE metrics(...)"}]
        cur.fetchall.side_effect = [[{"TABLE_NAME": "metrics", "TABLE_COMMENT": "password='abc'"}],
                                   [{"COLUMN_NAME": "metric_value", "DATA_TYPE": "decimal", "COLUMN_COMMENT": "值"}]]
        cur.fetchmany.return_value = [{"metric_value": 314159}, {"metric_value": None}]
        conn = Mock()
        conn.cursor.return_value = cur
        result = extract(cfg, conn)
        sample = next(b["content"] for b in result["blocks"] if b["block_type"] == "sample_profile")
        self.assertEqual(sample["exposed_values"], [])
        self.assertNotIn("314159", json.dumps(result))
        self.assertEqual(sample["profile"]["metric_value"]["null_count"], 1)
        self.assertNotIn("abc", json.dumps(result))
        self.assertTrue(result["coverage"]["complete"])
        self.assertFalse(any("INSERT" in call.args[0] for call in cur.execute.call_args_list))

    def test_redaction_preserves_structure(self):
        data = {"comment": "password='hidden'", "items": ["postgresql://u:hidden@db/x", "正常字段"]}
        sanitized = redact_values(data)
        self.assertNotIn("hidden", json.dumps(sanitized))
        self.assertEqual(sanitized["items"][1], "正常字段")

    @unittest.skipUnless(XLSX, "openpyxl unavailable")
    def test_all_sheets_rows_and_formula_text_survive(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "指标.xlsx"
            wb = openpyxl.Workbook()
            for i in range(5):
                sheet = wb.active if i == 0 else wb.create_sheet()
                sheet.title = "域" + str(i)
                sheet.append(["字段", "公式"])
                for n in range(600):
                    sheet.append(["指标" + str(n), "=SUM(A1:A2)"])
            wb.save(path)
            blocks, issues = parse(path, "doc")
            self.assertEqual(len(blocks), 5 * 601)
            self.assertEqual(len({b["locator"]["sheet"] for b in blocks}), 5)
            self.assertTrue(blocks[-1]["content"][1]["formula"])
            self.assertEqual(blocks[-1]["content"][1]["value"], "=SUM(A1:A2)")

    @unittest.skipUnless(SQL, "sqlglot unavailable")
    def test_git_commit_is_read_without_worktree_changes(self):
        from extract_repository import extract as extract_repo
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            def git(*args):
                return subprocess.run(["git", "-C", work, *args], check=True, capture_output=True)
            git("init")
            (root / "query.sql").write_text("SELECT region, SUM(amount) FROM sales GROUP BY region;\n")
            git("add", "query.sql")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@invalid", "commit", "-m", "fixture")
            (root / "query.sql").write_text("SELECT secret FROM uncommitted;\n")
            result = extract_repo(root, "HEAD", ["*.sql"], "code")
            text = json.dumps(result)
            self.assertIn("sales", text)
            self.assertNotIn("uncommitted", text)
            parsed = next(b["content"] for b in result["blocks"] if b["block_type"] == "sql_structure")
            self.assertEqual(parsed["statements"][0]["tables"], ["sales"])
            self.assertTrue(parsed["statements"][0]["aggregations"])

    def test_publisher_rejects_other_tenants_and_iri_injection(self):
        validate_graph_iri("https://arcp.sxmcc.com/tenant/chatbi/staging/abc")
        for graph in ("https://arcp.sxmcc.com/tenant/other", "https://arcp.sxmcc.com/tenant/chatbi/staging/x> } INSERT {}"):
            with self.assertRaises(ValueError):
                validate_graph_iri(graph)
        self.assertEqual(scalar({"type": "literal", "value": "9"}), 9)


class BundleTests(unittest.TestCase):
    def test_existing_graph_is_never_loaded_or_cleared(self):
        from pg_ripple import load_empty
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            artifacts = {}
            for name in TTL_FILES:
                data = b"# fixture\n"
                (root / name).write_bytes(data)
                artifacts[name] = {"sha256": sha256(data), "triples": 0}
            write_new(root / "manifest.json", {"artifacts": artifacts})
            review = {"manifest_sha256": sha256((root / "manifest.json").read_bytes()),
                      "approved": True, "cq_passed": True, "source_references_refreshed": True,
                      "authorization_reference": "test only"}
            conn = MagicMock()
            cur = conn.cursor.return_value.__enter__.return_value
            capabilities = {"functions": [{"name": "load_turtle_into_graph", "argument_types": "text, text"},
                                            {"name": "sparql", "argument_types": "text"}]}
            with patch("validate_rdf.check", return_value={"ok": True, "rdf": {"shacl": {"node_shapes": 1}}}), \
                 patch("pg_ripple.discover", return_value=capabilities), \
                 patch("pg_ripple.count_graph", return_value=5):
                with self.assertRaises(ValueError):
                    load_empty(conn, root, "https://arcp.sxmcc.com/tenant/chatbi", review)
            executed = [call.args[0] for call in cur.execute.call_args_list]
            self.assertFalse(any("load_turtle" in sql or "CLEAR" in sql or "DROP" in sql for sql in executed))
            self.assertIs(conn.transaction.return_value.__exit__.call_args.args[0], ValueError)

    def test_structural_hash_gate_without_optional_parsers(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            artifacts = {}
            for name in TTL_FILES:
                data = b"@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
                (root / name).write_bytes(data)
                artifacts[name] = {"sha256": sha256(data), "triples": 0}
            (root / "reports").mkdir()
            for name in ("source-inventory.md", "assumptions.md", "release-plan.md"):
                (root / "reports" / name).write_text("No supported assertions.\n")
            manifest = {"bundle_id": "empty", "bundle_version": "2.0.0", "generated_at": "2026-10-20T00:00:00Z",
                        "base_iri": BASE, "target_graph": "https://arcp.sxmcc.com/tenant/chatbi",
                        "operating_mode": "offline", "sources": [], "artifacts": artifacts,
                        "assertion_counts": {s: 0 for s in ("confirmed", "inferred", "candidate", "unresolved")},
                        "publication": {"status": "not_attempted"}}
            write_new(root / "manifest.json", manifest)
            self.assertTrue(validate(root)["ok"])
            (root / TTL_FILES[0]).write_text("# modified\n")
            self.assertIn("artifact_hash", {e["code"] for e in validate(root)["errors"]})

    def fixture(self, root):
        evidence = envelope("doc", "document", {}, [block("doc", "paragraph", {"line": 1}, "客户是业务实体")], {"complete": True})
        path = root / "evidence.json"
        write_new(path, evidence)
        ref = evidence["blocks"][0]["block_id"]
        decisions = {"bundle_id": "test", "bundle_version": "2.0.0", "base_iri": BASE,
                     "assertions": [{"file": TTL_FILES[0], "subject": BASE + "core/Customer",
                                     "predicate": "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                                     "object": {"iri": "http://www.w3.org/2002/07/owl#Class"},
                                     "status": "confirmed", "evidence": [ref]}]}
        return path, decisions

    @unittest.skipUnless(RDF and SHACL, "rdflib/pyshacl unavailable")
    def test_assembly_real_rdf_shacl_and_hash_tamper_gate(self):
        from assemble_bundle import assemble
        from validate_rdf import check
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            evidence, decisions = self.fixture(root)
            bundle = root / "bundle"
            assemble(decisions, [evidence], bundle)
            report = check(bundle, shacl=True)
            self.assertTrue(report["ok"], report)
            self.assertTrue(report["rdf"]["shacl"]["conforms"])
            manifest = json.loads((bundle / "manifest.json").read_text())
            self.assertEqual(manifest["assertion_counts"]["confirmed"], 1)
            with (bundle / TTL_FILES[0]).open("a") as stream:
                stream.write("# tampered\n")
            self.assertIn("artifact_hash", {error["code"] for error in validate(bundle)["errors"]})

    @unittest.skipUnless(RDF, "rdflib unavailable")
    def test_unknown_evidence_and_unconfirmed_axioms_fail(self):
        from assemble_bundle import assemble
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            evidence, decisions = self.fixture(root)
            decisions["assertions"][0]["evidence"] = ["invented"]
            with self.assertRaises(ValueError):
                assemble(decisions, [evidence], root / "invalid")
            _, valid = self.fixture_again(root)
            valid["assertions"][0].update(status="candidate", predicate="http://www.w3.org/2000/01/rdf-schema#subClassOf")
            with self.assertRaises(ValueError):
                assemble(valid, [evidence], root / "invalid2")

    def fixture_again(self, root):
        evidence = json.loads((root / "evidence.json").read_text())
        return root / "evidence.json", {"bundle_id": "test", "bundle_version": "2.0.0", "base_iri": BASE,
                "assertions": [{"file": TTL_FILES[0], "subject": BASE + "core/Customer",
                                "predicate": "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                                "object": {"iri": "http://www.w3.org/2002/07/owl#Class"},
                                "status": "confirmed", "evidence": [evidence["blocks"][0]["block_id"]]}]}


if __name__ == "__main__":
    unittest.main()
