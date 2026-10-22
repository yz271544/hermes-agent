#!/usr/bin/env python3
"""Parse explicit documents into located evidence; never evaluate formulas/macros."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

from evidence import block, envelope, redact, sha256, write_new


def parse(path: Path, sid: str) -> tuple[list, list]:
    suffix = path.suffix.lower()
    blocks, issues = [], []

    def add(kind, locator, content):
        # Redaction applies to all text, including Office cells.
        if isinstance(content, str):
            content = redact(content)
        blocks.append(block(sid, kind, {"file": path.name, **locator}, content))

    if suffix in {".md", ".txt", ".sql", ".ddl", ".json", ".yaml", ".yml", ".xml"}:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        for start in range(0, len(lines), 80):
            add("text", {"line_start": start + 1, "line_end": min(start + 80, len(lines))}, "\n".join(lines[start:start + 80]))
    elif suffix in {".csv", ".tsv"}:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for i, row in enumerate(csv.reader(stream, delimiter="\t" if suffix == ".tsv" else ","), 1):
                add("table_row", {"row": i}, [redact(cell) for cell in row])
    elif suffix == ".docx":
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        doc = Document(path)
        # Iterate body children to preserve paragraph/table order.
        for i, child in enumerate(doc.element.body, 1):
            if child.tag.endswith("}p"):
                paragraph = Paragraph(child, doc)
                if paragraph.text.strip():
                    add("paragraph", {"body_index": i, "style": paragraph.style.name if paragraph.style else None}, paragraph.text)
            elif child.tag.endswith("}tbl"):
                table = Table(child, doc)
                for j, row in enumerate(table.rows, 1):
                    add("table_row", {"body_index": i, "row": j}, [redact(cell.text) for cell in row.cells])
        issues.append({"status": "images_headers_footnotes_not_parsed", "action": "inspect relevant visual/ancillary content separately"})
    elif suffix == ".xlsx":
        import openpyxl
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=False, keep_links=False)
        try:
            for sheet in workbook:
                for row in sheet.iter_rows():
                    cells = [{"cell": cell.coordinate, "value": redact(str(cell.value)),
                              "formula": cell.data_type == "f"} for cell in row if cell.value is not None]
                    if cells:
                        add("spreadsheet_row", {"sheet": sheet.title, "row": row[0].row}, cells)
        finally:
            workbook.close()
        issues.append({"status": "images_and_formula_results_not_parsed", "action": "formula text retained; no recalculation"})
    elif suffix == ".pdf":
        import fitz
        with fitz.open(path) as document:
            for i, page in enumerate(document, 1):
                text = page.get_text(sort=True)
                if text.strip():
                    add("page_text", {"page": i}, text)
                else:
                    issues.append({"page": i, "status": "ocr_required"})
                if page.get_images():
                    issues.append({"page": i, "status": "images_require_visual_review"})
        issues.append({"status": "pdf_table_geometry_not_reconstructed", "action": "verify complex tables and formulas visually"})
    elif suffix == ".pptx":
        from pptx import Presentation
        for i, slide in enumerate(Presentation(path).slides, 1):
            for j, shape in enumerate(slide.shapes, 1):
                if shape.has_text_frame:
                    add("slide_text", {"slide": i, "shape": j}, shape.text)
                if shape.has_table:
                    for k, row in enumerate(shape.table.rows, 1):
                        add("table_row", {"slide": i, "shape": j, "row": k}, [redact(cell.text) for cell in row.cells])
            if slide.has_notes_slide:
                add("speaker_notes", {"slide": i}, slide.notes_slide.notes_text_frame.text)
        issues.append({"status": "pictures_charts_groups_require_visual_review"})
    else:
        raise ValueError("Unsupported format: convert .doc/.xls/.wps/.et/.dps locally, preserving the original and recording conversion provenance")
    return blocks, issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="Explicit authorized source files")
    parser.add_argument("--output", type=Path, required=True, help="Fresh output directory")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output directory")
    args.output.mkdir(parents=True)
    failed = False
    for index, path in enumerate(args.paths, 1):
        sid = "doc-" + str(index) + "-" + sha256(str(path.resolve()).encode())[:12]
        try:
            before = sha256(path.read_bytes())
            blocks, issues = parse(path, sid)
            if sha256(path.read_bytes()) != before:
                raise ValueError("Source changed during parsing")
            result = envelope(sid, "document", {"path": str(path.resolve()), "sha256": before}, blocks,
                              {"complete": not issues, "text_complete": True, "truncated": False, "issues": issues})
            failed = failed or bool(issues)
        except Exception as exc:
            failed = True
            result = envelope(sid, "document", {"path": str(path)}, [],
                              {"complete": False, "issues": [{"status": "parse_failed", "detail": type(exc).__name__}]})
        write_new(args.output / (sid + ".json"), result)
    print(str(args.output))
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
