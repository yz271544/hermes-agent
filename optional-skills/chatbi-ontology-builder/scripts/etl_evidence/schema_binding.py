"""Conservative star expansion using a single platform metadata snapshot."""
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers


def expand_stars(query, metadata, dialect):
    root = build_scope(query)
    if root is None:
        return []
    issues = []
    for scope in root.traverse():
        if not isinstance(scope.expression, exp.Select):
            continue
        select = scope.expression
        available = {a: source for a, (_, source) in scope.selected_sources.items()}
        projections = []
        for projection in select.selects:
            star = projection.this if isinstance(projection, exp.Column) and projection.is_star else projection
            if not isinstance(star, exp.Star):
                projections.append(projection)
                continue
            if any(star.args.values()):
                issues.append("wildcard_modifiers_require_review")
                projections.append(projection)
                continue
            qualifier = projection.table if isinstance(projection, exp.Column) else None
            sources = {qualifier: available.get(qualifier)} if qualifier else available
            # USING/NATURAL changes unqualified * layout and may coalesce join keys.
            if not qualifier and any(j.args.get("using") or j.args.get("method") == "NATURAL"
                                     for j in select.args.get("joins", [])):
                issues.append("wildcard_using_or_natural_join_requires_review")
                projections.append(projection)
                continue
            expanded, valid = [], bool(sources)
            for alias, source in sources.items():
                names = None
                if isinstance(source, exp.Table):
                    table, _ = metadata.resolve({"catalog": source.catalog, "database": source.db, "table": source.name})
                    columns = metadata.ordered_columns(table) if table else None
                    names = [c["column_name"] for c in columns] if columns else None
                elif isinstance(source, Scope):
                    selections = source.expression.selects
                    if not any(p.is_star for p in selections):
                        names = source.outer_columns or [p.alias_or_name for p in selections]
                if not names or not all(names):
                    valid = False
                    break
                expanded.extend(normalize_identifiers(exp.column(name, table=alias), dialect=dialect) for name in names)
            projections.extend(expanded if valid else [projection])
        select.set("expressions", projections)
    return issues
