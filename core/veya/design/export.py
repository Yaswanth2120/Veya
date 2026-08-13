"""Derived exports of `ArchitectureState`. JSON is the authoritative shape
already produced by `state.py`'s persistence; Mermaid/Markdown/PDF here are
always *derived* — never a second source of truth. No architecture text
is logged by anything in this module.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from .state import ArchitectureState, mermaid


def to_json(state: ArchitectureState) -> str:
    return json.dumps(asdict(state), indent=2, sort_keys=False)


def to_markdown(state: ArchitectureState) -> str:
    lines = [f"# {state.title}", "", f"_Version {state.version}_", ""]

    def section(title: str, items: list[str]) -> None:
        lines.append(f"## {title}")
        if not items:
            lines.append("_None_")
        else:
            lines.extend(f"- {item}" for item in items)
        lines.append("")

    lines.append("## Nodes")
    if not state.nodes:
        lines.append("_None_")
    else:
        lines.extend(f"- `{node.id}` — {node.label} ({node.kind})" for node in state.nodes)
    lines.append("")

    lines.append("## Edges")
    if not state.edges:
        lines.append("_None_")
    else:
        lines.extend(f"- `{edge.source}` → `{edge.target}`{f' ({edge.label})' if edge.label else ''}" for edge in state.edges)
    lines.append("")

    section("Requirements", state.requirements)
    section("Assumptions", state.assumptions)
    section("Decisions", state.decisions)
    section("Trade-offs", state.trade_offs)
    section("Risks", state.risks)
    section("Action Items", state.action_items)

    lines.append("## Diagram (Mermaid)")
    lines.append("```mermaid")
    lines.append(mermaid(state))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def to_pdf_bytes(state: ArchitectureState) -> bytes:
    """A minimal, dependency-free single/multi-page PDF renderer: plain
    text lines in Helvetica, wrapped onto additional pages as needed. Not
    a general PDF library — just enough valid PDF structure (objects,
    xref table, trailer) to produce a real, openable document from
    `to_markdown`'s content without a network fetch or a third-party
    dependency."""
    lines: list[str] = []
    for raw_line in to_markdown(state).splitlines():
        line = raw_line.replace("`", "").replace("#", "").strip() or " "
        # Hard-wrap long lines so they don't run off the page width.
        while len(line) > 95:
            lines.append(line[:95])
            line = line[95:]
        lines.append(line)

    lines_per_page = 54
    pages = [lines[i:i + lines_per_page] for i in range(0, len(lines), lines_per_page)] or [[" "]]

    objects: list[bytes] = []

    def add_object(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_obj = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    page_obj_numbers: list[int] = []
    content_obj_numbers: list[int] = []
    for page_lines in pages:
        stream_parts = ["BT", "/F1 10 Tf", "12 TL", "72 760 Td"]
        for line in page_lines:
            stream_parts.append(f"({_pdf_escape(line)}) Tj")
            stream_parts.append("0 -12 Td")
        stream_parts.append("ET")
        stream = "\n".join(stream_parts).encode("latin-1", errors="replace")
        content_obj_numbers.append(add_object(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))

    pages_obj_number = len(objects) + 1 + len(pages)  # reserved below

    for content_number in content_obj_numbers:
        page_body = (
            f"<< /Type /Page /Parent {pages_obj_number} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> /Contents {content_number} 0 R >>"
        ).encode("latin-1")
        page_obj_numbers.append(add_object(page_body))

    kids = " ".join(f"{n} 0 R" for n in page_obj_numbers)
    pages_body = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_obj_numbers)} >>".encode("latin-1")
    actual_pages_obj_number = add_object(pages_body)
    assert actual_pages_obj_number == pages_obj_number, "Pages object number reservation drifted."

    catalog_obj_number = add_object(f"<< /Type /Catalog /Pages {pages_obj_number} 0 R >>".encode("latin-1"))

    buffer = bytearray()
    buffer += b"%PDF-1.4\n"
    offsets = [0] * (len(objects) + 1)
    for index, body in enumerate(objects, start=1):
        offsets[index] = len(buffer)
        buffer += f"{index} 0 obj\n".encode("latin-1")
        buffer += body
        buffer += b"\nendobj\n"

    xref_offset = len(buffer)
    buffer += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    buffer += b"0000000000 65535 f \n"
    for index in range(1, len(objects) + 1):
        buffer += f"{offsets[index]:010d} 00000 n \n".encode("latin-1")
    buffer += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_obj_number} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    ).encode("latin-1")

    return bytes(buffer)
