"""Deterministic in-memory structural indexer for contract PDFs.

Week 1 utility. Bytes go in, clause records come out. Nothing is written to
disk, and this module does not import FastAPI or call a model.

Stages, in order:
1. Ingesta — PyMuPDF reads a PDF from memory and returns one text stream per page.
2. Normalización — layout noise is collapsed; letter case is left unchanged.
3. Conteo — each page keeps its chronological number and cleaned length.
4. Encabezados — a clause block is opened when the text starts with Artículo,
   Sección, or Cláusula. A block without one keeps the previous header.
5. Trazabilidad — each block is bound to the SHA-256 of its clean literal text.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass

import fitz

# Soft hyphens and zero-width marks are layout artifacts, not contract text.
_LAYOUT_CHARS = re.compile(r"[\u00ad\u200b\u200c\u200d\ufeff]")
# Form feeds, vertical tabs, and other C0 controls. Newlines are included so
# a page break cannot survive as its own character.
_CONTROLS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")

# Header name, copied from the text with its original casing.
# Identifier is a number (1, 1.2, 1º), a Spanish ordinal (PRIMERA, DÉCIMA
# PRIMERA), or a Roman numeral (IV, X).
_HEADER = re.compile(
    r"(?<![0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ])"
    r"(?P<head>"
    r"(?:art[ií]culo|secci[oó]n|cl[aá]usula)\.?"
    r"\s+"
    r"(?:"
    r"d[eé]cim[ao]\s+(?:primer|segund|tercer|cuart|quint|sext|s[eé]ptim|octav|noven)[ao]"
    r"|d[eé]cimo(?:primer|segund|tercer|cuart|quint|sext|s[eé]ptim|octav|noven)[ao]"
    r"|primer[ao]|segund[ao]|tercer[ao]|cuart[ao]|quint[ao]|sext[ao]"
    r"|s[eé]ptim[ao]|octav[ao]|noven[ao]|d[eé]cim[ao]"
    r"|und[eé]cim[ao]|duod[eé]cim[ao]|[uú]nic[ao]"
    r"|\d+(?:\.\d+)*[º°ª]?"
    r"|(?=[IVXLCDMivxlcdm])M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
    r")"
    r")"
    r"(?![0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ])",
    re.IGNORECASE,
)
# "el Artículo 5" cites a header; it does not open a new block.
_CITATION_LEAD = re.compile(r"(?i)(?:el|la|los|las|del|al)\s+$")

_CLAUSE_KEYS = (
    "clause_id",
    "articulo_seccion",
    "num_pagina",
    "texto_original_puro",
    "hash_trazabilidad",
)


@dataclass(frozen=True)
class PageRecord:
    """One PDF page after normalization, numbered from 1 in document order."""

    page_number: int
    text: str
    length: int


def ingest_pdf(payload: bytes) -> list[str]:
    """Extract a raw text stream for every page. The PDF stays in memory."""
    if not payload:
        raise ValueError("Empty PDF payload.")

    try:
        document = fitz.open(stream=payload, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — fitz raises several parser errors
        raise ValueError("Unable to read PDF bytes.") from exc

    try:
        if document.is_encrypted:
            raise ValueError("Encrypted PDFs are not indexed.")
        if document.page_count == 0:
            raise ValueError("PDF has no pages.")
        # sort=True fixes reading order from coordinates, independent of the
        # content stream, so the same file always yields the same text.
        return [page.get_text("text", sort=True) or "" for page in document]
    finally:
        document.close()


def normalize_text(text: str) -> str:
    """Collapse page breaks and repeated whitespace. Case is preserved."""
    without_layout = _LAYOUT_CHARS.sub("", text)
    without_controls = _CONTROLS.sub(" ", without_layout)
    collapsed = _WHITESPACE.sub(" ", without_controls)
    return collapsed.strip()


def index_pdf(payload: bytes) -> list[PageRecord]:
    """Ingest, normalize, and count every page in chronological order."""
    records: list[PageRecord] = []
    for page_number, raw in enumerate(ingest_pdf(payload), start=1):
        cleaned = normalize_text(raw)
        records.append(
            PageRecord(page_number=page_number, text=cleaned, length=len(cleaned))
        )
    return records


def _hash_trazabilidad(text: str) -> str:
    """SHA-256 hex digest of the clean literal text. Nothing else is hashed."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _opens_block(text: str, start: int) -> bool:
    """True when a header match is the start of a clause, not a citation."""
    if start == 0:
        return True
    if not text[start - 1].isspace():
        return False
    return _CITATION_LEAD.search(text[:start]) is None


def _blocks_on_page(text: str) -> list[tuple[str | None, str]]:
    """Split one cleaned page into (header or None, clause text) pairs."""
    if not text:
        return []
    starts = [match for match in _HEADER.finditer(text) if _opens_block(text, match.start())]
    if not starts:
        return [(None, text)]

    blocks: list[tuple[str | None, str]] = []
    if starts[0].start() > 0:
        preamble = text[: starts[0].start()].strip()
        if preamble:
            blocks.append((None, preamble))
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        body = text[match.start() : end].strip()
        if body:
            blocks.append((match.group("head").strip(), body))
    return blocks


def build_clause_tree(pages: list[PageRecord]) -> list[dict[str, object]]:
    """Clause dictionaries in reading order. Header context crosses page breaks."""
    clauses: list[dict[str, object]] = []
    articulo_seccion = ""
    clause_id = 1
    for page in pages:
        for header, body in _blocks_on_page(page.text):
            if header:
                articulo_seccion = header
            clauses.append(
                {
                    "clause_id": clause_id,
                    "articulo_seccion": articulo_seccion,
                    "num_pagina": page.page_number,
                    "texto_original_puro": body,
                    "hash_trazabilidad": _hash_trazabilidad(body),
                }
            )
            clause_id += 1
    return clauses


def index_clauses(payload: bytes) -> list[dict[str, object]]:
    """Ingest, normalize, detect headers, and bind each clause block."""
    return build_clause_tree(index_pdf(payload))


def build_test_pdf() -> bytes:
    """Two-page contract fixture. Returned as bytes; never written to disk."""
    document = fitz.open()
    try:
        first = document.new_page()
        first.insert_text(
            (72, 72),
            "CONTRATO de Servicios\n"
            "Articulo 1. El Cliente pagara el precio.\n"
            "Clausula PRIMERA. La vigencia sera de un ano.",
            fontsize=11,
        )
        second = document.new_page()
        second.insert_text(
            (72, 72),
            "El pago continua segun lo pactado.\n"
            "Seccion 2. Jurisdiccion: Ciudad de Mexico.",
            fontsize=11,
        )
        return document.tobytes()
    finally:
        document.close()


def _payload_from_args(argv: list[str]) -> bytes:
    if len(argv) > 2:
        raise SystemExit("usage: python indexer.py [file.pdf]")
    if len(argv) == 2:
        with open(argv[1], "rb") as handle:
            return handle.read()
    return build_test_pdf()


def _check_normalization() -> None:
    """Guard the case rule and the whitespace collapse on a fixed sample."""
    sample = "Cláusula  PRIMERA.\f\n\nEl   Cliente"
    cleaned = normalize_text(sample)
    expected = "Cláusula PRIMERA. El Cliente"
    if cleaned != expected:
        raise RuntimeError(f"normalization drifted: {cleaned!r}")


def _check_clause_tree() -> None:
    """Headers, inherited context, citation skips, and exact SHA-256 binding."""
    pages = [
        PageRecord(
            1,
            "Introduccion. Según el Artículo 9 no abre bloque. "
            "Artículo 4. Objeto. CLÁUSULA PRIMERA. Precio fijo. Sección X. Alcance.",
            0,
        ),
        PageRecord(2, "El precio se paga al firmar. Sección IV. Foro en Mexico.", 0),
    ]
    clauses = build_clause_tree(pages)
    headers = [clause["articulo_seccion"] for clause in clauses]
    expected_headers = [
        "",
        "Artículo 4",
        "CLÁUSULA PRIMERA",
        "Sección X",
        "Sección X",
        "Sección IV",
    ]
    if headers != expected_headers:
        raise RuntimeError(f"header tree drifted: {headers!r}")
    if [clause["num_pagina"] for clause in clauses] != [1, 1, 1, 1, 2, 2]:
        raise RuntimeError("page numbers drifted")
    if [clause["clause_id"] for clause in clauses] != [1, 2, 3, 4, 5, 6]:
        raise RuntimeError("clause ids drifted")
    for clause in clauses:
        if tuple(clause) != _CLAUSE_KEYS:
            raise RuntimeError(f"clause keys drifted: {tuple(clause)!r}")
        digest = _hash_trazabilidad(str(clause["texto_original_puro"]))
        if clause["hash_trazabilidad"] != digest:
            raise RuntimeError("hash is not the SHA-256 of texto_original_puro")
    carried = clauses[4]
    if carried["texto_original_puro"] != "El precio se paga al firmar.":
        raise RuntimeError("page-break continuation was not kept intact")


def main(argv: list[str] | None = None) -> list[dict[str, object]]:
    _check_normalization()
    _check_clause_tree()
    payload = _payload_from_args(list(sys.argv if argv is None else argv))
    clauses = index_clauses(payload)
    print(json.dumps(clauses, ensure_ascii=False, indent=2))
    return clauses


if __name__ == "__main__":
    main()
