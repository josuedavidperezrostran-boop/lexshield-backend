"""Cold exposure math for penalty JSON. No model calls and no I/O.

`valor_multa` and `base_calculo` are parsed locally. A unitless number or a
percent sign becomes a rate (`"25"` and `"25%"` are both `0.25`). When the
base names gross revenue or an annual pool, that rate is multiplied by a
fixed test revenue of $100,000. Exposure above $50,000, or gross revenue
with no cap, is forced to the tier CRÍTICO.
"""

from __future__ import annotations

import json
import re

TEST_REVENUE_BASE_USD = 100_000.0
CRITICAL_EXPOSURE_USD = 50_000.0
HIGH_EXPOSURE_USD = 10_000.0

_GROSS_REVENUE = "gross revenue"
_ANNUAL_POOL = "annual pool"

_PERCENT = re.compile(
    r"(?i)(\d[\d.,]*)\s*(?:%|％|percent\b|por\s*ciento|porcentaje\b)"
)
_MONEY = re.compile(
    r"(?i)(?:usd|us\$|\$|€|eur|£)\s*(\d[\d.,]*)|(\d[\d.,]*)\s*(?:usd|eur|dollars?)\b"
)
_NUMBER = re.compile(r"\d[\d.,]*")
_THOUSANDS = re.compile(r"\d{1,3}([,.]\d{3})+")

_NO_CAP = re.compile(
    r"(?i)\bno\s+cap\b|\bwithout(?:\s+a)?\s+cap\b|\buncapped\b|"
    r"\bsin\s+tope\b|\bsin\s+l[ií]mite\b|\bsin\s+cap\b"
)
_HAS_CAP = re.compile(
    r"(?i)\bcap(?:ped)?\b|\bceiling\b|\btope\b|\bl[ií]mite\b|"
    r"\bnot\s+to\s+exceed\b|\bm[aá]xim(?:o|um)\b"
)


def calculate_deterministic_exposure(extracted_data: dict) -> dict[str, object]:
    """Return raw liability and the hardcoded risk tier for one penalty object."""
    source = dict(extracted_data)
    rate, fee_usd = _parse_valor_multa(source.get("valor_multa"))
    base = _as_text(source.get("base_calculo")).casefold()
    uses_revenue_base = rate is not None and (
        _GROSS_REVENUE in base or _ANNUAL_POOL in base
    )
    if uses_revenue_base:
        exposure_usd = round(rate * TEST_REVENUE_BASE_USD, 2)
        revenue_base_usd: float | None = TEST_REVENUE_BASE_USD
    elif fee_usd is not None:
        exposure_usd = round(fee_usd, 2)
        revenue_base_usd = None
    else:
        exposure_usd = 0.0
        revenue_base_usd = None

    uncapped_gross_revenue = _GROSS_REVENUE in base and not _has_cap(source)
    risk_tier = _tier_from_exposure(exposure_usd)
    if exposure_usd > CRITICAL_EXPOSURE_USD or uncapped_gross_revenue:
        risk_tier = "CRÍTICO"

    clause_id = source.get("clause_id")
    return {
        "clause_id": clause_id if isinstance(clause_id, str) else None,
        "rate": rate,
        "fee_usd": None if fee_usd is None else round(fee_usd, 2),
        "exposure_usd": exposure_usd,
        "revenue_base_usd": revenue_base_usd,
        "uncapped_gross_revenue": uncapped_gross_revenue,
        "risk_tier": risk_tier,
    }


def _tier_from_exposure(exposure_usd: float) -> str:
    """Matrix applied before the CRÍTICO override."""
    if exposure_usd > CRITICAL_EXPOSURE_USD:
        return "CRÍTICO"
    if exposure_usd > HIGH_EXPOSURE_USD:
        return "ALTO"
    if exposure_usd > 0:
        return "MODERADO"
    return "BAJO"


def _parse_valor_multa(value: object) -> tuple[float | None, float | None]:
    """Return `(rate, fee_usd)`. A rate is a fraction, so 25% is 0.25."""
    text = _as_text(value)
    if not text:
        return None, None
    percent = _PERCENT.search(text)
    if percent:
        return _to_number(percent.group(1)) / 100.0, None
    money = _MONEY.search(text)
    if money:
        token = money.group(1) or money.group(2)
        return None, _to_number(token)
    number = _NUMBER.search(text)
    if number:
        return _to_number(number.group(0)) / 100.0, None
    return None, None


def _to_number(token: str) -> float:
    """Drop grouping marks. The last separator is the decimal point."""
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


def _has_cap(extracted_data: dict) -> bool:
    """True when the clause states a ceiling. An explicit no-cap phrase wins."""
    text = " ".join(
        _as_text(extracted_data.get(key))
        for key in ("valor_multa", "base_calculo", "cita_literal")
    )
    if _NO_CAP.search(text):
        return False
    return _HAS_CAP.search(text) is not None


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _check() -> list[dict[str, object]]:
    """Fixed cases for the rate parse, the revenue multiple, and the override."""
    cases = [
        (
            {"valor_multa": "25", "base_calculo": "5-year gross revenue", "clause_id": "1"},
            {"rate": 0.25, "exposure_usd": 25_000.0, "risk_tier": "CRÍTICO"},
        ),
        (
            {"valor_multa": "25%", "base_calculo": "5-year gross revenue", "clause_id": "1b"},
            {"rate": 0.25, "exposure_usd": 25_000.0, "risk_tier": "CRÍTICO"},
        ),
        (
            {
                "valor_multa": "10%",
                "base_calculo": "gross revenue capped at 12 months of fees",
                "clause_id": "2",
            },
            {"rate": 0.1, "exposure_usd": 10_000.0, "risk_tier": "MODERADO"},
        ),
        (
            {"valor_multa": "60%", "base_calculo": "annual pool", "clause_id": "3"},
            {"rate": 0.6, "exposure_usd": 60_000.0, "risk_tier": "CRÍTICO"},
        ),
        (
            {"valor_multa": "40%", "base_calculo": "annual pool", "clause_id": "4"},
            {"rate": 0.4, "exposure_usd": 40_000.0, "risk_tier": "ALTO"},
        ),
        (
            {"valor_multa": "$80,000", "base_calculo": "monthly retainer", "clause_id": "5"},
            {"rate": None, "fee_usd": 80_000.0, "exposure_usd": 80_000.0, "risk_tier": "CRÍTICO"},
        ),
        (
            {
                "valor_multa": None,
                "base_calculo": None,
                "clause_id": "6",
                "faltante": True,
            },
            {"rate": None, "exposure_usd": 0.0, "risk_tier": "BAJO"},
        ),
    ]
    results: list[dict[str, object]] = []
    for payload, expected in cases:
        result = calculate_deterministic_exposure(payload)
        results.append(result)
        for key, value in expected.items():
            if result[key] != value:
                raise RuntimeError(f"{payload['clause_id']} {key}: {result[key]!r} != {value!r}")
    return results


if __name__ == "__main__":
    print(json.dumps(_check(), ensure_ascii=False, indent=2))
