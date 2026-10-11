from __future__ import annotations

import difflib
import json
import re
from typing import Any

from fastapi import HTTPException

from .auditor import _client, _complete

_PAGE_MARK = re.compile(r"\[\[PÁGINA\s+\d+\]\]")
_SENTENCE = re.compile(r"(?<=[\.\;])\s+|\n+")
_ALERT_WORDS = (
    "responsab",
    "indemn",
    "jurisdic",
    "pena",
    "propiedad",
    "dolo",
    "precio",
    "confiden",
    "termin",
    "foro",
    "arbitra",
    "inter[eé]s",
    "interes",
    "código fuente",
    "codigo fuente",
    "negligencia",
    "limitaci",
)
_COMPARE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["resumen", "cambios"],
    "properties": {
        "resumen": {"type": "string"},
        "cambios": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "alerta", "analisis"],
                "properties": {
                    "id": {"type": "string"},
                    "alerta": {"type": "boolean"},
                    "analisis": {"type": "string"},
                },
            },
        },
    },
}


def compare_contracts(text_a: str, text_b: str) -> dict[str, Any]:
    """In-memory sentence diff. One classification pass labels the liability changes."""
    changes = _sentence_diff(text_a, text_b)
    if not changes:
        return {
            "resumen": "Las dos versiones no presentan diferencias de redacción.",
            "cambios": [],
        }
    ranked = sorted(changes, key=lambda item: (not item["alerta"], item["id"]))
    sample = ranked[:24]
    try:
        reviewed = _classify(sample)
    except HTTPException:
        reviewed = {}
    for change in changes:
        update = reviewed.get(change["id"])
        if not update:
            continue
        change["alerta"] = bool(update["alerta"]) or change["alerta"]
        if str(update.get("analisis") or "").strip():
            change["analisis"] = update["analisis"]
    alerts = sum(1 for change in changes if change["alerta"])
    return {
        "resumen": (
            f"Se detectaron {len(changes)} diferencias. {alerts} requieren alerta "
            "porque alteran pasivos, foro, precio o propiedad intelectual."
        ),
        "cambios": changes,
    }


def _classify(changes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    client = _client()
    payload = [
        {
            "id": change["id"],
            "tipo": change["tipo"],
            "texto_version_a": change["texto_version_a"],
            "texto_version_b": change["texto_version_b"],
        }
        for change in changes
    ]
    system = (
        "Eres el revisor de LexShield. Compara la versión A (original) con la versión B (modificada). "
        "alerta es true solo si el cambio incorpora, altera o suprime una responsabilidad, un foro, "
        "un precio, una pena, una limitación, dolo, negligencia grave o propiedad intelectual. "
        "analisis va en español jurídico profesional y dice qué se coló, qué se modificó o qué se eliminó. "
        "No invente texto que no esté en el par. Devuelva un objeto JSON."
    )
    raw = _complete(
        client,
        system,
        "Diferencias ya aisladas en memoria:\n" + json.dumps(payload, ensure_ascii=False),
        "lexshield_comparador",
        _COMPARE_SCHEMA,
    )
    parsed = json.loads(raw)
    items = parsed.get("cambios") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return {}
    return {str(item["id"]): item for item in items if isinstance(item, dict) and item.get("id")}


def _sentence_diff(text_a: str, text_b: str) -> list[dict[str, Any]]:
    left = _sentences(text_a)
    right = _sentences(text_b)
    changes: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            for sentence in left[i1:i2]:
                changes.append(_change("eliminado", sentence, ""))
        elif tag == "insert":
            for sentence in right[j1:j2]:
                changes.append(_change("agregado", "", sentence))
        else:
            removed = left[i1:i2]
            added = right[j1:j2]
            pairs = max(len(removed), len(added))
            for index in range(pairs):
                before = removed[index] if index < len(removed) else ""
                after = added[index] if index < len(added) else ""
                if before and after:
                    changes.append(_change("modificado", before, after))
                elif before:
                    changes.append(_change("eliminado", before, ""))
                elif after:
                    changes.append(_change("agregado", "", after))
    for index, change in enumerate(changes, start=1):
        change["id"] = f"dif-{index}"
    return changes


def _change(kind: str, original: str, modified: str) -> dict[str, Any]:
    blob = f"{original} {modified}".lower()
    alert = any(re.search(word, blob) for word in _ALERT_WORDS)
    if kind == "agregado":
        analysis = "La versión B incorpora esta frase, que no constaba en la versión A."
    elif kind == "eliminado":
        analysis = "La versión B suprime esta frase, que sí constaba en la versión A."
    else:
        analysis = "La versión B modifica esta frase respecto de la versión A."
    if alert:
        analysis += " El cambio toca pasivos, foro, precio o propiedad intelectual."
    return {
        "tipo": kind,
        "texto_version_a": original,
        "texto_version_b": modified,
        "alerta": alert,
        "analisis": analysis,
    }


def _sentences(text: str) -> list[str]:
    plain = _PAGE_MARK.sub(" ", text)
    parts = _SENTENCE.split(plain)
    return [" ".join(part.split()) for part in parts if len(" ".join(part.split())) >= 25]
