"""Cold integrity check for one extracted finding. No model calls and no UI.

The quote must appear, after layout normalization, inside the Week 1 block
with the same clause_id. A miss is withheld from the final output and placed
on cola_revision_humana. A Week 3 exposure that is greater than a liability
cap stated in an earlier block sets contradiccion_detectada.
"""

from __future__ import annotations

import json
import re

_LAYOUT_CHARS = re.compile(r"[\u00ad\u200b\u200c\u200d\ufeff]")
_CONTROLS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_LIABILITY_CAP = re.compile(
    r"(?i)(?:"
    r"liability\s+cap|aggregate\s+liability|total\s+liability|"
    r"limitation\s+of\s+liability|tope\s+de\s+responsabilidad|"
    r"l[ií]mite\s+de\s+responsabilidad|"
    r"responsabilidad\s+(?:m[aá]xima|total|agregada)"
    r")"
)
_MONEY = re.compile(
    r"(?i)(?:usd|us\$|\$|€|eur)\s*(\d[\d.,]*)|(\d[\d.,]*)\s*(?:usd|eur|dollars?)\b"
)
_THOUSANDS = re.compile(r"\d{1,3}([,.]\d{3})+")


def verify_extraction_integrity(
    original_blocks: list,
    extracted_finding: dict,
) -> dict[str, object]:
    """Accept a finding only when its quote is in the indexed clause."""
    if not isinstance(original_blocks, list):
        raise TypeError("original_blocks must be a list of indexed clauses.")
    if not isinstance(extracted_finding, dict):
        raise TypeError("extracted_finding must be a dict.")

    finding = dict(extracted_finding)
    verified = _quote_matches_clause(original_blocks, finding)
    definitions = _early_liability_cap(original_blocks, finding.get("clause_id"))
    exposure = _exposure_usd(finding.get("exposure_usd"))
    cap = definitions["liability_cap_usd"]
    contradiccion_detectada = (
        exposure is not None and cap is not None and exposure > cap
    )
    if verified:
        salida_final: list[dict] = [finding]
        cola_revision_humana: list[dict] = []
    else:
        salida_final = []
        cola_revision_humana = [finding]
    return {
        "verified": verified,
        "contradiccion_detectada": contradiccion_detectada,
        "mapa_definiciones": definitions,
        "salida_final": salida_final,
        "cola_revision_humana": cola_revision_humana,
    }


def _quote_matches_clause(blocks: list, finding: dict) -> bool:
    quote = _normalize(_as_text(finding.get("cita_literal")))
    if not quote:
        return False
    matches = [
        block
        for block in blocks
        if isinstance(block, dict) and _same_id(block.get("clause_id"), finding.get("clause_id"))
    ]
    if not matches:
        return False
    return any(quote in _normalize(_as_text(block.get("texto_original_puro"))) for block in matches)


def _early_liability_cap(blocks: list, clause_id: object) -> dict[str, object]:
    """Most restrictive liability cap stated before the penalty clause."""
    chosen_amount: float | None = None
    chosen_id: str | None = None
    for block in blocks:
        if not isinstance(block, dict) or not _is_earlier(block, blocks, clause_id):
            continue
        text = _as_text(block.get("texto_original_puro"))
        if not _LIABILITY_CAP.search(text):
            continue
        amount = _first_money(text)
        if amount is None:
            continue
        if chosen_amount is None or amount < chosen_amount:
            chosen_amount = amount
            chosen_id = _id_key(block.get("clause_id")) or None
    return {"liability_cap_usd": chosen_amount, "clause_id": chosen_id}


def _is_earlier(block: dict, blocks: list, clause_id: object) -> bool:
    block_key = _id_key(block.get("clause_id"))
    target_key = _id_key(clause_id)
    if block_key.isdigit() and target_key.isdigit():
        return int(block_key) < int(target_key)
    target_at = None
    block_at = None
    for index, item in enumerate(blocks):
        if not isinstance(item, dict):
            continue
        if item is block:
            block_at = index
        if target_at is None and _same_id(item.get("clause_id"), clause_id):
            target_at = index
    if target_at is None or block_at is None:
        return False
    return block_at < target_at


def _first_money(text: str) -> float | None:
    match = _MONEY.search(text)
    if not match:
        return None
    token = match.group(1) or match.group(2)
    return round(_to_number(token), 2)


def _to_number(token: str) -> float:
    cleaned = token.strip().replace(" ", "")
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(".") > cleaned.rfind(","):
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        if _THOUSANDS.fullmatch(cleaned):
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")
    elif _THOUSANDS.fullmatch(cleaned):
        cleaned = cleaned.replace(".", "")
    return float(cleaned)


def _exposure_usd(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _normalize(text: str) -> str:
    """Strip edges and collapse layout whitespace. Letter case stays as written."""
    without_layout = _LAYOUT_CHARS.sub("", text)
    without_controls = _CONTROLS.sub(" ", without_layout)
    return _WHITESPACE.sub(" ", without_controls).strip()


def _same_id(left: object, right: object) -> bool:
    key = _id_key(left)
    return bool(key) and key == _id_key(right)


def _id_key(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        return value.strip()
    return ""


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _check() -> list[dict[str, object]]:
    blocks = [
        {
            "clause_id": 1,
            "texto_original_puro": "Aggregate liability shall not exceed $50,000.",
        },
        {
            "clause_id": 2,
            "texto_original_puro": "The Client shall pay a penalty of 60% of the annual pool.",
        },
        {
            "clause_id": 3,
            "texto_original_puro": "Confidentiality survives termination.",
        },
    ]
    quote = "The Client shall pay a penalty of 60% of the annual pool."
    over = verify_extraction_integrity(
        blocks,
        {
            "clause_id": "2",
            "cita_literal": "  The   Client shall pay a penalty of 60% of the annual pool. ",
            "exposure_usd": 60_000,
        },
    )
    under = verify_extraction_integrity(
        blocks,
        {"clause_id": "2", "cita_literal": quote, "exposure_usd": 40_000},
    )
    wrong_clause = verify_extraction_integrity(
        blocks,
        {"clause_id": "3", "cita_literal": quote, "exposure_usd": 60_000},
    )
    wrong_case = verify_extraction_integrity(
        blocks,
        {"clause_id": 2, "cita_literal": quote.lower(), "exposure_usd": 60_000},
    )
    same_clause_cap = verify_extraction_integrity(
        [
            {"clause_id": 1, "texto_original_puro": "The Client and the Vendor are the parties."},
            {
                "clause_id": 2,
                "texto_original_puro": (
                    "Aggregate liability shall not exceed $10,000. The Client shall pay $80,000."
                ),
            },
        ],
        {
            "clause_id": "2",
            "cita_literal": "The Client shall pay $80,000.",
            "exposure_usd": 80_000,
        },
    )
    checks = [
        (over["verified"] is True, "spaced quote should match clause 2"),
        (over["contradiccion_detectada"] is True, "60000 must exceed the early 50000 cap"),
        (over["salida_final"] != [] and over["cola_revision_humana"] == [], "verified finding stays publishable"),
        (under["verified"] is True and under["contradiccion_detectada"] is False, "40000 is inside the cap"),
        (wrong_clause["verified"] is False, "a quote from another clause is not evidence"),
        (wrong_clause["salida_final"] == [] and len(wrong_clause["cola_revision_humana"]) == 1, "failed quote is withheld"),
        (wrong_clause["cola_revision_humana"][0]["cita_literal"] == quote, "the queued finding is intact"),
        (wrong_case["verified"] is False, "case must match word for word"),
        (same_clause_cap["verified"] is True and same_clause_cap["contradiccion_detectada"] is False, "a cap in the penalty clause is not an earlier definition"),
    ]
    for passed, message in checks:
        if not passed:
            raise RuntimeError(message)
    return [over, under, wrong_clause, same_clause_cap]


if __name__ == "__main__":
    print(json.dumps(_check(), ensure_ascii=False, indent=2))
