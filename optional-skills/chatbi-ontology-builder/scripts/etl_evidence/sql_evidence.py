"""Static SQL evidence, separate from legacy LOGDBINFO and business ontology decisions."""
from __future__ import annotations

import hashlib
import re
from bisect import bisect_left
from typing import Iterator

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from .distributed_rewriter import strip_distributed

PARSER_VERSION = "sql-evidence-1"
DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_]\w*)?\$")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def split_sql(text: str) -> Iterator[dict]:
    """Quote/comment-aware splitting; offsets and line ranges address the original file.

    SQL comments and BTEQ dot commands are blanked, not removed, preserving newlines.
    Dollar-quoted procedural bodies are not supported and explicitly rejected.
    """
    cleaned = list(text)
    line_breaks = [i for i, char in enumerate(text) if char == "\n"]
    state, quote, start, i = "normal", "", 0, 0
    line_start = True
    while i < len(text):
        char = text[i]
        pair = text[i:i + 2]
        if state == "dollar":
            if text.startswith(quote, i):
                state = "normal"
                i += len(quote) - 1
        elif state == "line":
            if char == "\n":
                state = "normal"
            else:
                cleaned[i] = " "
        elif state == "block":
            if pair == "*/":
                cleaned[i:i + 2] = [" ", " "]
                state = "normal"
                i += 1
            elif char != "\n":
                cleaned[i] = " "
        elif state == "quote":
            if char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    i += 1
                else:
                    state = "normal"
            elif char == "\\" and i + 1 < len(text):
                i += 1
        elif pair in ("--", "/*"):
            cleaned[i:i + 2] = [" ", " "]
            state = "line" if pair == "--" else "block"
            i += 1
        elif line_start and char == "." and text[i + 1:i + 2].isalpha():
            cleaned[i] = " "
            state = "line"
        elif char in ("'", '"', "`"):
            quote, state = char, "quote"
        elif char == "$" and (match := DOLLAR_QUOTE.match(text, i)):
            quote, state = match.group(), "dollar"
            i += len(quote) - 1
        elif char == ";":
            yield from _fragment(text, cleaned, start, i, None, line_breaks)
            start = i + 1
        if char == "\n":
            line_start = True
        elif not char.isspace():
            line_start = False
        i += 1
    issue = "unterminated_quote_or_comment" if state in ("quote", "block", "dollar") else None
    yield from _fragment(text, cleaned, start, len(text), issue, line_breaks)


def _fragment(text, cleaned, start, end, issue, line_breaks):
    sql = "".join(cleaned[start:end]).strip()
    if sql or issue:
        yield {"raw_sql": text[start:end], "sql": sql,
               "offset_start": start, "offset_end": end,
               "line_start": bisect_left(line_breaks, start) + 1,
               "line_end": bisect_left(line_breaks, end) + 1,
               "lexical_issue": issue}


def table_identity(table: exp.Table) -> dict:
    return {"catalog": table.catalog or None, "database": table.db or None,
            "table": table.name, "binding": "unverified"}


def table_key(identity: dict) -> str:
    return ".".join(str(identity[k]) for k in ("catalog", "database", "table") if identity.get(k)).upper()


def _wildcard_projection(expression):
    expression = expression.this if isinstance(expression, exp.Alias) else expression
    return isinstance(expression, exp.Star) or (isinstance(expression, exp.Column) and expression.is_star)


def analyze_sql(sql: str, dialect: str = "teradata", metadata=None) -> dict:
    result = {"status": "parsed", "issues": [], "tables": [], "scopes": [],
              "transformations": [], "physical_binding": "unverified"}
    normalized = strip_distributed(sql)
    result["parsed_sql"] = normalized
    result["rewrites"] = ["strip_distributed"] if normalized != sql else []
    if metadata:
        result["metadata_snapshot"] = metadata.identity

    def identity(table):
        value = table_identity(table)
        if metadata:
            physical, issue = metadata.resolve(value)
            if issue:
                result["issues"].append(issue + ":" + table_key(value))
            else:
                value.update(binding="catalog_snapshot", platform=metadata.data["platform"],
                             metadata_table_id=physical["table_id"], schema=physical.get("schema_name"),
                             description=physical.get("table_desc"))
        return value

    def source_column(table, name):
        value = dict(identity(table), column=name)
        if metadata:
            physical, _ = metadata.resolve(value)
            column = metadata.column(physical, name) if physical else None
            if column:
                value["column_metadata"] = column
            elif physical:
                value["binding"] = "column_unverified"
                result["issues"].append("column_not_in_metadata:" + table_key(value) + "." + name)
        return value
    if DOLLAR_QUOTE.search(sql) or "{{" in sql or "{%" in sql:
        result.update(status="unsupported", issues=["procedural_or_dynamic_template"])
        return result
    try:
        ast = sqlglot.parse_one(normalized, dialect=dialect, error_level=sqlglot.ErrorLevel.RAISE)
        if ast is None or isinstance(ast, exp.Command):
            result.update(status="unsupported", issues=["unsupported_statement"])
            return result
        ast = normalize_identifiers(ast, dialect=dialect)
        result["operation"] = ast.key.upper()
        target_node = ast.this if isinstance(ast, (exp.Insert, exp.Create, exp.Update, exp.Delete)) else None
        target_columns = []
        if isinstance(target_node, exp.Schema):
            target_columns = [c.name for c in target_node.expressions]
            target_node = target_node.this
        target = identity(target_node) if isinstance(target_node, exp.Table) else None
        target_metadata = metadata.resolve(target)[0] if metadata and target else None
        if isinstance(ast, exp.Insert) and target_metadata and target_columns:
            for name in target_columns:
                if metadata.column(target_metadata, name) is None:
                    result["issues"].append("target_column_not_in_metadata:" + name)
        if isinstance(ast, exp.Insert) and metadata and target and not target_columns:
            columns = metadata.ordered_columns(target_metadata) if target_metadata else None
            if columns:
                target_columns = [normalize_identifiers(exp.to_identifier(c["column_name"]), dialect=dialect).name
                                  for c in columns]
                result["target_column_order_source"] = "catalog_snapshot.ordinal"
        if target:
            result["tables"].append(dict(target, role="write"))
        query = ast.expression if isinstance(ast, (exp.Insert, exp.Create)) else ast
        while isinstance(query, exp.Subquery):
            query = query.this
        if metadata and isinstance(query, exp.Query):
            from .schema_binding import expand_stars
            result["issues"].extend(expand_stars(query, metadata, dialect))
            result["bound_sql"] = query.sql(dialect=dialect)
        root = build_scope(query) if isinstance(query, exp.Query) else None
        if root is None:
            for table in ast.find_all(exp.Table):
                if table is not target_node:
                    result["tables"].append(dict(identity(table), role="reference"))
            if isinstance(ast, (exp.Update, exp.Delete, exp.Merge)):
                result["issues"].append("dml_transformations_not_supported")
            result["ddl"] = ast.sql(dialect=dialect) if isinstance(ast, exp.Create) else None
        else:
            scopes = list(root.traverse())
            ids = {id(scope): f"q{i + 1}" for i, scope in enumerate(scopes)}
            memo = {}

            def selected(scope):
                return {alias: source for alias, (_, source) in scope.selected_sources.items()}

            def union_scopes(scope):
                return getattr(scope, "set_operation_scopes", getattr(scope, "union_scopes", []))

            def projection_sources(scope, index, active):
                key = (id(scope), index)
                if key in memo:
                    return memo[key]
                if key in active:
                    return [], ["recursive_cte_or_scope_cycle"]
                active = active | {key}
                if union_scopes(scope):
                    sources, issues = [], []
                    for branch in union_scopes(scope):
                        s, e = projection_sources(branch, index, active)
                        sources.extend(s)
                        issues.extend(e)
                    return unique(sources), sorted(set(issues))
                selects = scope.expression.selects
                if index >= len(selects):
                    return [], ["projection_count_mismatch"]
                expression = selects[index]
                sources, issues = [], []
                if _wildcard_projection(expression):
                    issues.append("wildcard_requires_schema")
                for column in expression.find_all(exp.Column):
                    # A scalar subquery has its own scope; do not bind its columns here.
                    if column.find_ancestor(exp.Select) is not scope.expression:
                        issues.append("scalar_subquery_requires_review")
                        continue
                    qualifier = column.table
                    available = selected(scope)
                    choices = [available.get(qualifier)] if qualifier else list(available.values())
                    if metadata and not qualifier and len(choices) > 1:
                        known, matching = True, []
                        for candidate in choices:
                            if isinstance(candidate, exp.Table):
                                physical, _ = metadata.resolve(table_identity(candidate))
                                if physical is None:
                                    known = False
                                    break
                                if metadata.column(physical, column.name):
                                    matching.append(candidate)
                            elif isinstance(candidate, Scope):
                                names = candidate.outer_columns or [p.alias_or_name for p in candidate.expression.selects]
                                if any(p.is_star for p in candidate.expression.selects):
                                    known = False
                                    break
                                if column.name in names:
                                    matching.append(candidate)
                        if known:
                            choices = matching
                    if len(choices) != 1 or choices[0] is None:
                        issues.append("ambiguous_or_unbound_column:" + column.sql())
                        continue
                    source = choices[0]
                    if isinstance(source, exp.Table):
                        sources.append(source_column(source, column.name))
                    elif isinstance(source, Scope):
                        # Explicit CTE/derived column lists override projection aliases.
                        names = source.outer_columns or [s.alias_or_name for s in source.expression.selects]
                        matches = [i for i, name in enumerate(names) if name == column.name]
                        if len(matches) != 1:
                            issues.append("unresolved_derived_column:" + column.sql())
                        else:
                            s, e = projection_sources(source, matches[0], active)
                            sources.extend(s)
                            issues.extend(e)
                memo[key] = unique(sources), sorted(set(issues))
                return memo[key]

            for scope in scopes:
                for source in selected(scope).values():
                    if isinstance(source, exp.Table):
                        result["tables"].append(dict(identity(source), role="read"))
                if not isinstance(scope.expression, exp.Select):
                    continue
                select = scope.expression
                projections = []
                for i, projection in enumerate(select.selects):
                    sources, issues = projection_sources(scope, i, set())
                    result["issues"].extend(issues)
                    expression = projection.this if isinstance(projection, exp.Alias) else projection
                    projections.append({"position": i + 1, "output_name": projection.alias_or_name or None,
                                        "expression": expression.sql(dialect=dialect), "sources": sources,
                                        "aggregations": [a.sql(dialect=dialect) for a in expression.find_all(exp.AggFunc)],
                                        "windows": [w.sql(dialect=dialect) for w in expression.find_all(exp.Window)],
                                        "issues": issues})
                result["scopes"].append({
                    "scope_id": ids[id(scope)], "sql": select.sql(dialect=dialect),
                    "distinct": bool(select.args.get("distinct")),
                    "group_by": [g.sql(dialect=dialect) for g in select.args.get("group", exp.Group()).expressions],
                    "where": _sql(select.args.get("where"), dialect),
                    "having": _sql(select.args.get("having"), dialect),
                    "projections": projections,
                    "joins": [{"type": " ".join(filter(None, [j.side, j.kind])) or "INNER",
                               "relation": j.this.sql(dialect=dialect),
                               "condition": _sql(j.args.get("on"), dialect),
                               "using": [c.sql(dialect=dialect) for c in j.args.get("using", [])],
                               "cardinality": "unknown"} for j in select.args.get("joins", [])],
                })

            def branches(scope):
                if union_scopes(scope):
                    for child in union_scopes(scope):
                        yield from branches(child)
                else:
                    yield scope

            if isinstance(ast, exp.Create) and not target_columns:
                names = [p.alias or (p.name if isinstance(p, exp.Column) else None)
                         for p in root.expression.selects]
                if names and all(names):
                    target_columns = names
            unsupported_set = any(not isinstance(node, exp.Union) for node in query.find_all(exp.SetOperation))
            if unsupported_set:
                result["issues"].append("except_or_intersect_requires_review")
            for branch_no, branch in enumerate(branches(root), 1):
                if unsupported_set:
                    break
                if not isinstance(branch.expression, exp.Select):
                    result["issues"].append("query_form_not_supported")
                    continue
                selections = branch.expression.selects
                if target and not target_columns:
                    result["issues"].append("target_columns_require_schema")
                if target_columns and len(target_columns) != len(selections):
                    result["issues"].append("target_projection_count_mismatch")
                positional_binding = (len(target_columns) == len(selections) and
                                      not any(_wildcard_projection(p) for p in selections))
                for i, projection in enumerate(selections):
                    sources, issues = projection_sources(branch, i, set())
                    expression = projection.this if isinstance(projection, exp.Alias) else projection
                    result["transformations"].append({
                        "scope_id": ids[id(branch)], "branch": branch_no, "position": i + 1,
                        "target_table": target,
                        "target_column": target_columns[i] if positional_binding else None,
                        "target_column_metadata": metadata.column(target_metadata, target_columns[i])
                        if metadata and target_metadata and positional_binding else None,
                        "output_name": projection.alias_or_name or None,
                        "expression": expression.sql(dialect=dialect), "sources": sources,
                        "aggregations": [a.sql(dialect=dialect) for a in expression.find_all(exp.AggFunc)],
                        "windows": [w.sql(dialect=dialect) for w in expression.find_all(exp.Window)],
                        "status": "partial" if issues else "syntactic",
                        "issues": issues,
                    })
                    result["issues"].extend(issues)
        result["tables"] = unique(result["tables"])
        if metadata:
            result["physical_binding"] = "catalog_snapshot_partial" if any(
                t["binding"] != "catalog_snapshot" for t in result["tables"]) else "catalog_snapshot"
        result["issues"] = sorted(set(result["issues"]))
        if result["issues"]:
            result["status"] = "partial"
    except Exception as exc:
        # Keep source and visible failure; do not label empty output as successful.
        result.update(status="error", issues=[type(exc).__name__ + ": " + str(exc)[:500]])
    return result


def _sql(node, dialect):
    return node.sql(dialect=dialect) if node is not None else None


def unique(items):
    import json
    return list({json.dumps(item, sort_keys=True): item for item in items}.values())
