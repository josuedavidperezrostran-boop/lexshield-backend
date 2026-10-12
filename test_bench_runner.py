"""Localized simulation of the cold LexShield pipeline.

Three stress-test clauses are ingested as data, then each one is passed on
its own through the calculator, the evidence verifier, and the redactor.
Nothing here calls a model or opens a route.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE / "backend"
sys.path.insert(0, str(_BACKEND if (_BACKEND / "math_validator.py").is_file() else _HERE))

from evidence_verifier import verify_extraction_integrity
from legal_redactor import generate_safe_alternative_clause
from math_validator import calculate_deterministic_exposure

STRESS_CONTRACT: list[dict[str, object]] = [
    {
        "name": "Block A (The Penalty Trap)",
        "clause_id": "SEC-22.04",
        "texto_original_puro": (
            "The Service Provider shall pay a 25% fee on 5-year gross revenue "
            "with a 3-day deadline."
        ),
        "cita_literal": "25% fee on 5-year gross revenue with a 3-day deadline",
        "valor_multa": "25%",
        "base_calculo": "5-year gross revenue",
        "faltante": False,
    },
    {
        "name": "Block B (The Privacy Trap)",
        "clause_id": "SEC-09.01",
        "texto_original_puro": (
            "The Processor shall maintain mandatory compliance under "
            "European GDPR and California CCPA regulations."
        ),
        "cita_literal": "European GDPR and California CCPA regulations",
        "valor_multa": None,
        "base_calculo": None,
        "faltante": True,
        "normativas_detectadas": ["GDPR", "CCPA"],
        "faltante_privacidad": False,
        "intention": "global data privacy compliance",
    },
    {
        "name": "Block C (The Hallucination Bait)",
        "clause_id": "SEC-21.00",
        "texto_original_puro": (
            "The Supplier shall maintain a 5 million euro insurance policy liability boundary."
        ),
        "cita_literal": "5 million euro insurance policy liability boundary",
        "valor_multa": "€5,000,000",
        "base_calculo": "insurance policy liability boundary",
        "faltante": False,
    },
]

_EXTRACT_KEYS = (
    "clause_id",
    "cita_literal",
    "valor_multa",
    "base_calculo",
    "faltante",
    "normativas_detectadas",
    "faltante_privacidad",
    "intention",
)


def _indexed_blocks(contract: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "clause_id": block["clause_id"],
            "texto_original_puro": block["texto_original_puro"],
        }
        for block in contract
    ]


def _extract_one(block: dict[str, object]) -> dict[str, object]:
    """Week 2-style variables taken only from this block."""
    return {key: block[key] for key in _EXTRACT_KEYS if key in block}


def _run_block(
    block: dict[str, object],
    original_blocks: list[dict[str, object]],
) -> dict[str, object]:
    extracted = _extract_one(block)
    math = calculate_deterministic_exposure(extracted)
    finding = {
        **extracted,
        "exposure_usd": math["exposure_usd"],
        "risk_tier": math["risk_tier"],
    }
    verification = verify_extraction_integrity(original_blocks, finding)
    draft = generate_safe_alternative_clause(verification)
    return {"math": math, "verification": verification, "draft": draft}


def _report(results: list[dict[str, object]]) -> tuple[bool, bool, bool]:
    print("LexShield pipeline simulation")
    print("-" * 72)
    for block, result in zip(STRESS_CONTRACT, results, strict=True):
        math = result["math"]
        verification = result["verification"]
        draft = result["draft"]
        assert isinstance(math, dict)
        assert isinstance(verification, dict)
        assert isinstance(draft, dict)
        print(f"{block['name']}")
        print(f"  clause_id: {block['clause_id']}")
        print(f"  tier: {math['risk_tier']}")
        print(f"  uncapped_gross_revenue: {math['uncapped_gross_revenue']}")
        print(f"  rate: {math['rate']}")
        print(f"  fee_usd: {math['fee_usd']}")
        print(f"  exposure_usd: {math['exposure_usd']}")
        print(f"  revenue_base_usd: {math['revenue_base_usd']}")
        print(f"  verified: {verification['verified']}")
        print(f"  contradiccion_detectada: {verification['contradiccion_detectada']}")
        print(f"  draft: {draft['texto_reemplazo_word']}")
        print()

    math_a = results[0]["math"]
    math_c = results[2]["math"]
    draft_b = results[1]["draft"]
    source_b = str(STRESS_CONTRACT[1]["texto_original_puro"])
    replacement_b = str(draft_b["texto_reemplazo_word"])
    assert isinstance(math_a, dict)
    assert isinstance(math_c, dict)

    penalty_is_critical = (
        math_a["risk_tier"] == "CRÍTICO" and math_a["uncapped_gross_revenue"] is True
    )
    privacy_locked = (
        "GDPR" in source_b
        and "CCPA" in source_b
        and "GDPR/CCPA" in replacement_b
        and "Costa Rica" not in replacement_b
        and "tribunales de" not in replacement_b
        and "plantilla de foro del playbook" in replacement_b
    )
    bait_is_isolated = (
        math_c["fee_usd"] == 5_000_000.0
        and math_c["rate"] != math_a["rate"]
        and math_c["exposure_usd"] != math_a["exposure_usd"]
        and math_c["revenue_base_usd"] is None
        and math_c["uncapped_gross_revenue"] is False
    )

    print("Tolerancia Cero")
    print("-" * 72)
    print(
        "Did the calculator successfully route Block A to CRÍTICO "
        f"(uncapped revenue) based on cold python code? {penalty_is_critical}"
    )
    print(
        "Did the playbook override automatically lock out and preserve the "
        f"GDPR/CCPA text on Block B? {privacy_locked}"
    )
    print(
        "Did Block C successfully report a clean financial extraction without "
        f"crossing math parameters with Block A? {bait_is_isolated}"
    )
    return penalty_is_critical, privacy_locked, bait_is_isolated


def main() -> None:
    original_blocks = _indexed_blocks(STRESS_CONTRACT)
    results = [_run_block(block, original_blocks) for block in STRESS_CONTRACT]
    penalty_is_critical, privacy_locked, bait_is_isolated = _report(results)
    assert penalty_is_critical, "Block A was not forced to CRÍTICO"
    assert privacy_locked, "Block B did not lock out a local jurisdiction swap"
    assert bait_is_isolated, "Block C reused Block A math parameters"


if __name__ == "__main__":
    main()
