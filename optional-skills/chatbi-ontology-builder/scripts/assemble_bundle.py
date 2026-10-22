#!/usr/bin/env python3
"""Assemble explicit, evidence-backed LLM/reviewer decisions into the fixed TTL bundle."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from evidence import load_config, sha256, write_new
from validate_bundle import TTL_FILES, STATUSES


def assemble(decisions: dict, evidence_paths: list[Path], output: Path) -> dict:
    from rdflib import Graph, URIRef, Literal, Namespace
    from rdflib.namespace import RDF, OWL, RDFS
    if output.exists():
        raise ValueError("Use a new bundle directory; reviewed incremental decisions should be merged before assembly")
    base = decisions["base_iri"]
    if not base.startswith("https://arcp.sxmcc.com/") or not base.endswith("/"):
        raise ValueError("Use an arcp.sxmcc.com project base ending in /")
    sources, blocks = {}, {}
    for path in evidence_paths:
        evidence = load_config(path)
        sid = evidence["source_id"]
        if sid in sources:
            raise ValueError("Duplicate source_id")
        sources[sid] = {"source_id": sid, "type": evidence["source_type"], "sha256": sha256(path.read_bytes()),
                        "coverage": evidence["coverage"], "extracted_at": evidence["extracted_at"]}
        for item in evidence["blocks"]:
            if item["block_id"] in blocks:
                raise ValueError("Duplicate block id")
            blocks[item["block_id"]] = (sid, item["locator"])
    graphs = {name: Graph() for name in TTL_FILES}
    gov, src = Namespace(base + "governance/"), Namespace(base + "source/")
    prov = Namespace("http://www.w3.org/ns/prov#")
    counts = Counter()
    seen = set()
    for item in decisions["assertions"]:
        name = item["file"]
        if name not in graphs or item["status"] not in STATUSES or not item.get("evidence"):
            raise ValueError("Each assertion needs a valid file, status, and evidence block IDs")
        if any(ref not in blocks for ref in item["evidence"]):
            raise ValueError("Unknown evidence block ID")
        subject, predicate = URIRef(item["subject"]), URIRef(item["predicate"])
        obj_spec = item["object"]
        if set(obj_spec) == {"iri"}:
            obj = URIRef(obj_spec["iri"])
        elif "value" in obj_spec and not (obj_spec.get("lang") and obj_spec.get("datatype")):
            obj = Literal(obj_spec["value"], lang=obj_spec.get("lang"), datatype=obj_spec.get("datatype"))
        else:
            raise ValueError("Object must be {iri} or {value, lang?/datatype?}")
        for term in (subject, predicate, obj):
            if isinstance(term, URIRef) and (not str(term).startswith(("https://", "http://", "urn:")) or any(c in str(term) for c in '<>"{}|\\^` \n\r\t')):
                raise ValueError("Invalid absolute IRI")
        if not str(subject).startswith(base):
            raise ValueError("Project assertion subjects must use the configured project base")
        logical_axiom = (str(predicate).startswith(str(OWL)) or predicate in {RDFS.subClassOf, RDFS.subPropertyOf, RDFS.domain, RDFS.range})
        if item["status"] != "confirmed" and (name == TTL_FILES[5] or logical_axiom):
            raise ValueError("Unconfirmed logical axioms/mappings belong in review, not asserted RDF")
        triple = (subject, predicate, obj)
        if triple in seen:
            raise ValueError("Duplicate triple: merge evidence/status in decisions first")
        seen.add(triple)
        graph = graphs[name]
        graph.add(triple)
        statement = gov["assertion-" + sha256(json.dumps([str(t) for t in triple] + [obj.n3()], ensure_ascii=False).encode())[:24]]
        for p, o in ((RDF.type, RDF.Statement), (RDF.subject, subject), (RDF.predicate, predicate),
                     (RDF.object, obj), (gov.assertionStatus, Literal(item["status"])),
                     (gov.bundleVersion, Literal(decisions["bundle_version"]))):
            graph.add((statement, p, o))
        for ref in item["evidence"]:
            fragment = src["block-" + sha256(ref.encode())[:24]]
            graph.add((statement, prov.wasDerivedFrom, fragment))
        counts[item["status"]] += 1
    # Evidence identities/locators are metadata ABox. Raw source text and rows stay outside RDF.
    evidence_graph = graphs[TTL_FILES[3]]
    for ref in sorted({ref for item in decisions["assertions"] for ref in item["evidence"]}):
        sid, locator = blocks[ref]
        fragment = src["block-" + sha256(ref.encode())[:24]]
        evidence_graph.add((fragment, RDF.type, prov.Entity))
        evidence_graph.add((fragment, gov.sourceId, Literal(sid)))
        evidence_graph.add((fragment, gov.blockId, Literal(ref)))
        evidence_graph.add((fragment, gov.locator, Literal(json.dumps(locator, ensure_ascii=False, sort_keys=True))))
        evidence_graph.add((fragment, gov.sourceChecksum, Literal(sources[sid]["sha256"])))
    output.mkdir(parents=True)
    artifacts = {}
    for name, graph in graphs.items():
        graph.bind("gov", gov)
        graph.bind("src", src)
        # Sorted N-Triples is valid Turtle and byte-stable (no generated blank nodes).
        data = "# Turtle bundle artifact\n" + "\n".join(sorted(graph.serialize(format="nt").splitlines())) + "\n"
        (output / name).write_text(data, encoding="utf-8")
        artifacts[name] = {"sha256": sha256(data.encode()), "triples": len(graph)}
    manifest = {"contract_version": "1.1", "bundle_id": decisions["bundle_id"], "bundle_version": decisions["bundle_version"],
                "generated_at": datetime.now(timezone.utc).isoformat(), "base_iri": base,
                "target_graph": "https://arcp.sxmcc.com/tenant/chatbi", "operating_mode": "offline",
                "sources": list(sources.values()), "artifacts": artifacts,
                "assertion_counts": {status: counts[status] for status in sorted(STATUSES)},
                "assertion_count_basis": "rdf_statements", "publication": {"status": "not_attempted"}}
    write_new(output / "manifest.json", manifest)
    for name, content in {"source-inventory.md": json.dumps(list(sources.values()), ensure_ascii=False, indent=2),
                          "assumptions.md": decisions.get("review_notes", "Pending semantic review; inspect assertion evidence and CQ results."),
                          "release-plan.md": "Not published. Refresh source references; run RDF/SHACL, business/CQ review, then discover target capabilities."}.items():
        path = output / "reports" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = assemble(load_config(args.decisions), args.evidence, args.output)
        print(json.dumps({"output": str(args.output), "assertion_counts": manifest["assertion_counts"]}))
        return 0
    except (ValueError, KeyError, ImportError, OSError) as exc:
        print("Assembly failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
