from __future__ import annotations

import json
import re
from pathlib import Path

_PLAYBOOK_PATH = Path(__file__).resolve().parents[1] / "playbook.json"
_FORUM_CUE = re.compile(
    r"(?i)jurisdicci[oó]n|ley aplicable|leyes aplicables|derecho aplicable|tribunales|juzgados|"
    r"sede arbitral|foro exclusivo|governing law|courts of"
)
_PLACE = re.compile(
    r"(?i)(?:tribunales|juzgados|cortes|sede arbitral|sede|foro|leyes|derecho|jurisdicci[oó]n)"
    r"\s+(?:exclusiv[oa]s?\s+)?(?:de|del|en|of)\s+([^.\n;]{3,80})"
)
_LIMIT_CUE = re.compile(
    r"(?i)limitaci[oó]n de responsabilidad|responsabilidad\s+(?:total\s+)?(?:se\s+)?limit|"
    r"tope de responsabilidad|liability cap|no exceder[aá]"
)
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


def load_playbook() -> dict[str, str]:
    """Firm rules read before every analysis. Defaults match the certified playbook."""
    fallback = {
        "Jurisdiccion_Permitida": "Costa Rica",
        "Limite_Responsabilidad_Aceptable": "100%",
    }
    try:
        loaded = json.loads(_PLAYBOOK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    if not isinstance(loaded, dict):
        return fallback
    for key, value in fallback.items():
        if not str(loaded.get(key) or "").strip():
            loaded[key] = value
    return {key: str(loaded[key]) for key in fallback}


def acceptable_liability_percent(playbook: dict[str, str] | None = None) -> float:
    book = playbook or load_playbook()
    raw = book["Limite_Responsabilidad_Aceptable"].replace("%", "").strip()
    parsed = _parse_percent(raw)
    return parsed if parsed is not None else 100.0


def jurisdiction_violations(
    sections: list[tuple[str, str]],
    allowed: str,
) -> list[tuple[str, str]]:
    allowed_norm = allowed.casefold()
    found: list[tuple[str, str]] = []
    for article, body in sections:
        if not article or not _FORUM_CUE.search(body):
            continue
        places = [match.group(1).strip(" ,") for match in _PLACE.finditer(body)]
        foreign = [place for place in places if allowed_norm not in place.casefold()]
        if foreign:
            found.append((article, foreign[0]))
            continue
        if not places and allowed_norm not in body.casefold():
            found.append((article, f"foro distinto de {allowed}"))
    return found


def liability_violations(
    sections: list[tuple[str, str]],
    acceptable: float,
) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    for article, body in sections:
        if not article or not _LIMIT_CUE.search(body):
            continue
        short: list[float] = []
        for match in _PERCENT.finditer(body):
            value = _parse_percent(match.group(1))
            if value is None or value >= acceptable:
                continue
            window = body[max(0, match.start() - 90) : match.end() + 40].lower()
            if any(word in window for word in ("responsab", "honorari", "liability", "tope")):
                short.append(value)
        if short:
            found.append((article, min(short)))
    return found


def _parse_percent(raw: str) -> float | None:
    value = raw.replace(" ", "")
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        tail = value.split(",")[-1]
        value = value.replace(",", ".") if value.count(",") == 1 and len(tail) in (1, 2) else value.replace(",", "")
    elif value.count(".") > 1 or ("." in value and len(value.split(".")[-1]) == 3):
        value = value.replace(".", "")
    try:
        amount = float(value)
    except ValueError:
        return None
    return amount if amount > 0 else None
