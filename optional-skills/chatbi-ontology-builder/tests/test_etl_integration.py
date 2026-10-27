"""Standalone ETL + prompt + multi-source behavior, using temporary inputs only."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
from etl_evidence.sql_evidence import analyze_sql, split_sql
from etl_evidence.store import collect, export, query
from etl_evidence.git_snapshot import materialize
from etl_evidence.metadata import PlatformMetadata, make_snapshot
from evidence import block, envelope, write_new
from extract_prompt import extract as prompt_extract
from prepare_review import prepare


def metadata(source="etl", platform=100, target="uid"):
    tables = []
    for tid, name, names in [(1, "a", ["id", "value"]), (2, "t", [target, "total"])]:
        tables.append({"table_id": tid, "plat_id": platform, "database_name": "db", "table_name": name,
                       "catalog_name": None, "schema_name": "db", "table_desc": name,
                       "columns": [{"column_id": i, "column_name": n, "ordinal": str(i),
                                    "column_origin_type": "bigint", "column_desc": "说明" + n}
                                   for i, n in enumerate(names, 1)]})
    return PlatformMetadata(make_snapshot(source, "gbase8a", {"plat_id": platform, "name": "gbase-test"},
                                         tables, ["DB.A", "DB.T"]))


class StaticEvidenceTests(unittest.TestCase):
    def test_expression_scope_and_variable_preservation(self):
        sql = ("INSERT INTO ads.t(n) SELECT COUNT(DISTINCT CASE WHEN a.status='A' THEN a.id END) "
               "FROM db.a a WHERE a.deal_date=CAST('${TxDate}' AS DATE) GROUP BY a.region")
        result = analyze_sql(sql)
        self.assertEqual(result["status"], "parsed", result)
        item = result["transformations"][0]
        self.assertEqual(item["target_column"], "n")
        self.assertEqual({s["column"] for s in item["sources"]}, {"id", "status"})
        self.assertIn("${TxDate}", result["scopes"][0]["where"])

    def test_quote_aware_split_and_unsupported_procedure(self):
        parts = list(split_sql(".LOGON ignored;\nSELECT ';--/*' x; SELECT 'it''s;ok' y;"))
        self.assertEqual(len(parts), 2)
        self.assertIn("';--/*'", parts[0]["sql"])
        parts = list(split_sql("CREATE FUNCTION f() AS $x$ SELECT 1; SELECT 2; $x$; SELECT 3;"))
        self.assertEqual(len(parts), 2)
        self.assertEqual(analyze_sql(parts[0]["sql"])["status"], "unsupported")

    def test_platform_star_order_and_unknown_source(self):
        md = metadata()
        result = analyze_sql("INSERT INTO db.t SELECT * FROM db.a", metadata=md)
        self.assertEqual(result["status"], "parsed", result)
        self.assertEqual([t["target_column"] for t in result["transformations"]], ["uid", "total"])
        self.assertEqual(result["transformations"][0]["sources"][0]["column"], "id")
        md.data["tables"][0]["columns"][0]["ordinal"] = "4"
        self.assertIsNone(md.ordered_columns(md.data["tables"][0]))
        result = analyze_sql("SELECT id FROM db.a a JOIN db.unknown b ON a.id=b.id", metadata=md)
        self.assertIn("ambiguous_or_unbound_column:id", result["issues"])

    def test_cte_union_and_missing_target_list(self):
        result = analyze_sql("INSERT INTO ads.t(x) WITH c(v) AS (SELECT id FROM db.a) "
                             "SELECT v FROM c UNION ALL SELECT id FROM db.b")
        self.assertEqual(result["status"], "parsed", result)
        self.assertEqual([r["sources"][0]["table"] for r in result["transformations"]], ["a", "b"])
        self.assertEqual(analyze_sql("INSERT INTO ads.t SELECT id FROM db.a")["status"], "partial")

    def test_real_etl_baseline_preserves_known_and_unknown_outputs(self):
        import hashlib
        fixtures = SKILL / "tests/fixtures/sql_evidence"
        for item in json.loads((fixtures / "manifest.json").read_text()):
            raw = (fixtures / item["fixture"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), item["fixture_sha256"])
            result = analyze_sql(next(split_sql(raw.decode()))["sql"])
            self.assertEqual(result["status"], "partial", result["issues"])
            if item["fixture"] == "real_ctas.sql":
                self.assertEqual(len(result["transformations"]), 11)
            else:
                self.assertEqual(len(result["transformations"]), 1680)
                self.assertTrue(all(t["target_column"] is None for t in result["transformations"]))

    def test_metadata_change_rebind_and_platform_lock(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work) / "sql"
            root.mkdir()
            (root / "x.sql").write_text("INSERT INTO db.t SELECT * FROM db.a")
            path, index = Path(work) / "metadata.json", Path(work) / "etl.sqlite"
            path.write_text(json.dumps(metadata().data))
            collect(root, index, "etl", engine="gbase8a", metadata_path=path)
            self.assertEqual(collect(root, index, "etl", engine="gbase8a", metadata_path=path)["cached_files"], 1)
            path.write_text(json.dumps(metadata(target="user_id").data))
            self.assertEqual(collect(root, index, "etl", engine="gbase8a", metadata_path=path)["processed_files"], 1)
            self.assertEqual(query(index, "etl", "db.t")["statements"][0]["transformations"][0]["target_column"], "user_id")
            path.write_text(json.dumps(metadata(platform=200).data))
            with self.assertRaisesRegex(ValueError, "another metadata platform"):
                collect(root, index, "etl", engine="gbase8a", metadata_path=path)


class GitEvidenceTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout.decode().strip()

    def commit(self, root):
        self.git(root, "add", ".")
        self.git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@invalid", "commit", "-m", "fixture")
        return self.git(root, "rev-parse", "HEAD")

    def test_fixed_commit_cache_refresh_and_untracked_snapshot_exclusion(self):
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            repo = work / "repo"
            repo.mkdir()
            self.git(repo, "init")
            (repo / "job.sql").write_text("INSERT INTO ads.t(x) SELECT id FROM db.a;")
            first = self.commit(repo)
            (repo / "job.sql").write_text("SELECT secret FROM uncommitted;")
            root, origin = materialize(repo, first, work / "snapshots", "etl", ["*.sql"], 100000, 10)
            (root / "untracked.sql").write_text("SELECT secret FROM unexpected;")
            index = work / "etl.sqlite"
            result = collect(root, index, "etl", origin=origin)
            self.assertEqual(result["processed_files"], 1)
            item = query(index, "etl", "ads.t")["statements"][0]
            self.assertEqual(item["commit"], first)
            self.assertNotIn("secret", item["raw_sql"])
            # A new commit with the same SQL blob refreshes locator without re-parsing SQL.
            (repo / "job.sql").write_text("INSERT INTO ads.t(x) SELECT id FROM db.a;")
            (repo / "notes.txt").write_text("new version")
            second = self.commit(repo)
            root, origin = materialize(repo, second, work / "snapshots", "etl", ["*.sql"], 100000, 10)
            result = collect(root, index, "etl", origin=origin)
            self.assertEqual(result["cached_files"], 1)
            out = export(index, "etl", "ads.t", work / "evidence.json")
            self.assertEqual(out["blocks"][0]["locator"]["commit"], second)
            self.assertEqual(out["identity"]["read_mode"], "git_commit")

    def test_git_budget_omission_visible_and_materialized_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            repo = work / "repo"
            repo.mkdir()
            self.git(repo, "init")
            (repo / "ok.sql").write_text("SELECT id FROM db.a")
            (repo / "huge.sql").write_text("SELECT '" + "x" * 200 + "' FROM db.b")
            commit = self.commit(repo)
            root, origin = materialize(repo, commit, work / "snapshots", "etl", ["*.sql"], 100, 10)
            index = work / "etl.sqlite"
            self.assertFalse(collect(root, index, "etl", max_file_bytes=100, origin=origin)["complete"])
            self.assertFalse(export(index, "etl", "db.a", work / "out.json")["coverage"]["complete"])
            (root / "ok.sql").write_text("SELECT changed FROM db.a")
            with self.assertRaisesRegex(ValueError, "snapshot changed"):
                collect(root, index, "etl", max_file_bytes=100, origin=origin)
            self.assertFalse(query(index, "etl", "db.a")["scan_finished"])


class MultiSourceTests(unittest.TestCase):
    def test_prompt_is_located_claim_not_confirmed_fact(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "definition.txt"
            path.write_text("请整理客户本体。\n\n活跃用户按月去重。")
            evidence = prompt_extract(path, "owner", "business_definition", "owner", "message-1")
            self.assertEqual(len(evidence["blocks"]), 2)
            self.assertEqual(evidence["blocks"][1]["locator"]["line_start"], 3)
            self.assertEqual(evidence["blocks"][0]["content"]["verification"], "requires_reconciliation")
            evidence_path = Path(work) / "prompt.json"
            write_new(evidence_path, evidence)
            with self.assertRaisesRegex(ValueError, "Candidate budget"):
                prepare([evidence_path], Path(work) / "review", max_candidates=1)
            self.assertFalse((Path(work) / "review").exists())

    def test_multi_table_export_and_review_account_for_every_source_block(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            sql = root / "sql"
            sql.mkdir()
            (sql / "a.sql").write_text("INSERT INTO ads.t(x) SELECT id FROM db.a; INSERT INTO ads.u(y) SELECT id FROM db.b;")
            index = root / "etl.sqlite"
            collect(sql, index, "etl")
            etl = root / "etl.json"
            data = export(index, "etl", ["ads.t", "ads.u"], etl)
            self.assertEqual(len(data["blocks"]), 2)
            doc = root / "doc.json"
            write_new(doc, envelope("document", "document", {}, [block("document", "paragraph", {"line": 1}, "客户是业务实体")], {"complete": True}))
            prompt = root / "prompt.txt"
            prompt.write_text("请把客户作为分析对象，单位暂未确认。")
            user = root / "user.json"
            write_new(user, prompt_extract(prompt, "user"))
            output = root / "review"
            result = prepare([etl, doc, user], output)
            self.assertEqual(result["sources"], 3)
            self.assertEqual(result["blocks"], 4)
            ledger = json.loads((output / "merge-ledger.json").read_text())
            self.assertTrue(all(e["disposition"] == "pending_semantic_review" for e in ledger))
            self.assertFalse(result["ontology_generated"])
            candidates = json.loads((output / "candidates.json").read_text())
            self.assertTrue(all(c["status"] == "candidate" for c in candidates))
            with self.assertRaises(ValueError):
                prepare([etl], root / "too-big", max_blocks=1)
            self.assertFalse((root / "too-big").exists())

    def test_relocated_skill_cli_has_no_project_dependency(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            relocated = root / "skill"
            shutil.copytree(SKILL, relocated, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
            source = root / "sql"
            source.mkdir()
            (source / "x.sql").write_text("SELECT id FROM db.a")
            result = subprocess.run([sys.executable, "-B", str(relocated / "scripts/etl.py"), "scan", str(source),
                                     "--source-id", "etl", "--index", str(root / "etl.sqlite")],
                                    cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["processed_files"], 1)

    @unittest.skipUnless(importlib.util.find_spec("rdflib") and importlib.util.find_spec("pyshacl"), "RDF/SHACL dependencies missing")
    def test_reviewed_multisource_decisions_assemble_and_validate(self):
        from assemble_bundle import assemble
        from validate_rdf import check
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            doc = root / "doc.json"
            evidence = envelope("document", "document", {}, [block("document", "paragraph", {"line": 1}, "客户是业务实体；本体客户类必须有中文名称。")], {"complete": True})
            write_new(doc, evidence)
            prompt = root / "prompt.txt"
            prompt.write_text("本轮指标名称为活跃用户数，单位及其他属性待确认。")
            user = root / "user.json"
            user_data = prompt_extract(prompt, "user", "business_definition")
            write_new(user, user_data)
            catalog = root / "catalog.json"
            catalog_block = block("warehouse", "table_metadata", {"database": "db", "table": "a"},
                                  {"table_comment": "用户明细表", "columns": [{"name": "id", "type": "bigint"}]})
            write_new(catalog, envelope("warehouse", "database", {"engine": "gbase8a"}, [catalog_block], {"complete": True}))
            sql = root / "sql"
            sql.mkdir()
            (sql / "metric.sql").write_text("INSERT INTO ads.t(n) SELECT COUNT(DISTINCT id) FROM db.a")
            collect(sql, root / "etl.sqlite", "etl")
            etl_path = root / "etl.json"
            etl_data = export(root / "etl.sqlite", "etl", "ads.t", etl_path)
            inputs = [doc, user, catalog, etl_path]
            prepare(inputs, root / "review")
            base = "https://arcp.sxmcc.com/zb_data/chatbi/"
            doc_ref = evidence["blocks"][0]["block_id"]
            decisions = {"bundle_id": "fixture", "bundle_version": "2.1.0", "base_iri": base,
                         "assertions": [{"file": "01-core.owl.ttl", "subject": base + "core/Customer",
                          "predicate": "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                          "object": {"iri": "http://www.w3.org/2002/07/owl#Class"},
                          "status": "confirmed", "evidence": [doc_ref]}]}
            def assertion(file, subject, predicate, obj, ref, status="confirmed"):
                decisions["assertions"].append({"file": file, "subject": base + subject,
                    "predicate": predicate, "object": obj, "status": status, "evidence": [ref]})
            rdfs = "http://www.w3.org/2000/01/rdf-schema#"
            sh = "http://www.w3.org/ns/shacl#"
            rdf_type = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
            assertion("01-core.owl.ttl", "core/Customer", rdfs + "label", {"value": "客户", "lang": "zh"}, doc_ref)
            assertion("04-data-capability.ttl", "capability/UsersTable", rdfs + "label",
                      {"value": "用户明细表", "lang": "zh"}, catalog_block["block_id"])
            assertion("02-domain-metric.ttl", "metric/ActiveUsers", rdfs + "label",
                      {"value": "活跃用户数", "lang": "zh"}, user_data["blocks"][0]["block_id"], "candidate")
            assertion("02-domain-metric.ttl", "metric/ActiveUsers", base + "dw/formula",
                      {"value": "COUNT(DISTINCT id)"}, etl_data["blocks"][0]["block_id"], "candidate")
            for subject, predicate, obj in [
                ("governance/CustomerShape", rdf_type, {"iri": sh + "NodeShape"}),
                ("governance/CustomerShape", sh + "targetNode", {"iri": base + "core/Customer"}),
                ("governance/CustomerShape", sh + "property", {"iri": base + "governance/CustomerLabel"}),
                ("governance/CustomerLabel", sh + "path", {"iri": rdfs + "label"}),
                ("governance/CustomerLabel", sh + "minCount", {"value": 1, "datatype": "http://www.w3.org/2001/XMLSchema#integer"}),
            ]:
                assertion("07-shapes.ttl", subject, predicate, obj, doc_ref)
            manifest = assemble(decisions, inputs, root / "bundle")
            self.assertEqual(len(manifest["sources"]), 4)
            report = check(root / "bundle", shacl=True)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["rdf"]["shacl"]["node_shapes"], 1)


if __name__ == "__main__":
    unittest.main()
