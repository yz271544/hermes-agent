#!/usr/bin/env python3
"""Parse the seven Turtle graphs, verify counts/references and optionally run SHACL."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

from validate_bundle import TTL_FILES, validate


def local(term) -> str:
    return str(term).rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def check(root: Path, shacl=False) -> dict:
    from rdflib import Graph, URIRef
    from rdflib.namespace import RDF, OWL
    report = validate(root)
    if not report["ok"]:
        return report
    manifest = json.loads((root / "manifest.json").read_text())
    artifacts = manifest["artifacts"]
    if isinstance(artifacts, list):
        artifacts = {item["path"]: item for item in artifacts}
    errors = report["errors"]
    union, graphs, owners = Graph(), {}, {}
    for name in TTL_FILES:
        graph = Graph()
        try:
            graph.parse(root / name, format="turtle")
        except Exception as exc:
            errors.append({"code": "rdf_syntax", "path": name, "message": type(exc).__name__})
            continue
        graphs[name] = graph
        if len(graph) != artifacts[name]["triples"]:
            errors.append({"code": "triple_count", "path": name})
        for triple in graph:
            if triple in owners:
                errors.append({"code": "duplicate_triple", "path": name, "other": owners[triple]})
            owners[triple] = name
            union.add(triple)
            for term in triple:
                if isinstance(term, URIRef) and str(term).startswith("https://chatbi.sxmcc.com/"):
                    errors.append({"code": "old_namespace", "path": name})
    if len(graphs) != len(TTL_FILES):
        report["ok"] = False
        return report
    subjects = set(union.subjects())
    reference_predicates = {"mapsToTable", "mapsToColumn", "sourceColumn", "sourceClass", "usesMetric", "leftColumn", "rightColumn"}
    project = "https://arcp.sxmcc.com/"
    for subject, predicate, obj in union:
        if local(predicate) in reference_predicates and isinstance(obj, URIRef) and str(obj).startswith(project) and obj not in subjects:
            errors.append({"code": "dangling_reference", "resource": str(subject), "target": str(obj)})
        if predicate == RDF.type and isinstance(obj, URIRef) and str(obj).startswith(project):
            if (obj, RDF.type, OWL.Class) not in union:
                errors.append({"code": "undeclared_local_class", "target": str(obj)})
    counts = Counter()
    uncertain = set()
    critical = {"type", "mapsToTable", "mapsToColumn", "primaryKey", "grain", "sourceColumn",
                "aggregation", "formula", "unit", "timeGrain", "leftColumn", "rightColumn",
                "joinsTo", "dataType", "cardinality"}
    from rdflib import Namespace
    prov = Namespace("http://www.w3.org/ns/prov#")
    for statement in union.subjects(RDF.type, RDF.Statement):
        pieces = [list(union.objects(statement, predicate)) for predicate in (RDF.subject, RDF.predicate, RDF.object)]
        statuses = [obj for predicate, obj in union.predicate_objects(statement) if local(predicate) == "assertionStatus"]
        if any(len(values) != 1 for values in pieces) or len(statuses) != 1:
            errors.append({"code": "malformed_assertion", "resource": str(statement)})
        elif (pieces[0][0], pieces[1][0], pieces[2][0]) not in union:
            errors.append({"code": "assertion_triple_missing", "resource": str(statement)})
        origins = list(union.objects(statement, prov.wasDerivedFrom))
        if not origins or any(origin not in subjects for origin in origins):
            errors.append({"code": "assertion_provenance_missing", "resource": str(statement)})
    for subject, predicate, obj in union:
        if local(predicate) == "assertionStatus":
            if str(obj) not in {"confirmed", "candidate", "inferred", "unresolved"}:
                errors.append({"code": "assertion_status", "resource": str(subject)})
            if (subject, RDF.type, RDF.Statement) in union:
                counts[str(obj)] += 1
                if str(obj) != "confirmed":
                    predicates = list(union.objects(subject, RDF.predicate))
                    if any(local(p) in critical for p in predicates):
                        uncertain.update(union.objects(subject, RDF.subject))
                        uncertain.update(union.objects(subject, RDF.object))
            elif str(obj) != "confirmed":
                uncertain.add(subject)  # Legacy resource-level status.
    if manifest.get("assertion_count_basis") == "rdf_statements":
        if dict(counts) != {k: v for k, v in manifest["assertion_counts"].items() if v}:
            errors.append({"code": "assertion_count_mismatch"})
    else:
        report["warnings"].append({"code": "legacy_count_basis", "message": "State counts need manual audit; new bundles use rdf_statements"})
    mapping = graphs["06-mapping.r2rml.ttl"]
    if any(term in uncertain for triple in mapping for term in triple):
        errors.append({"code": "uncertain_executable_mapping"})
    report["rdf"] = {"triples_by_file": {name: len(graph) for name, graph in graphs.items()},
                     "union_triples": len(union), "shacl": "not_run"}
    if shacl and not errors:
        from pyshacl import validate as shacl_validate
        data = Graph()
        for name in TTL_FILES[:-1]:
            data += graphs[name]
        conforms, result_graph, _ = shacl_validate(data, shacl_graph=graphs[TTL_FILES[-1]],
                                                  inference="none", advanced=False,
                                                  allow_warnings=False, allow_infos=False)
        from rdflib.namespace import SH
        shape_count = len(set(graphs[TTL_FILES[-1]].subjects(RDF.type, SH.NodeShape)))
        report["rdf"]["shacl"] = {"conforms": bool(conforms), "node_shapes": shape_count,
                                    "results": len(list(result_graph.subjects(RDF.type, SH.ValidationResult)))}
        if not conforms:
            errors.append({"code": "shacl_violation"})
        if not shape_count:
            report["warnings"].append({"code": "no_node_shapes", "message": "SHACL ran with no NodeShapes; semantic review is still required"})
    report["ok"] = not errors
    report["note"] = "RDF and declared checks only; source semantics and CQ review remain required."
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--shacl", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = check(args.bundle, args.shacl)
    except (ImportError, OSError, ValueError) as exc:
        print(type(exc).__name__ + ": RDF validation could not complete", file=sys.stderr)
        return 1
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
