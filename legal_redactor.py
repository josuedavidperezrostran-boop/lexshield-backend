"""Isolated redraft for one Week 4 verified payload. No routes and no model calls.

The only input is the dictionary returned by the evidence verifier. Raw
contract blocks are rejected. Drafting follows the severity matrix and, when
the finding is marked GDPR or CCPA, does not apply a local forum swap.
"""

from __future__ import annotations

import json

# Instruction lock. It is not sent to a model with contract text attached.
ISOLATION_PROMPT = (
    "Use only the verified payload. Do not request, re-read, or ingest raw "
    "contract chunks, pages, or texto_original_puro. "
    "If the calculated severity is CRÍTICO, the replacement must remove the "
    "penalty layout or impose a hard fee cap. "
    "If the finding is global data privacy compliance (GDPR/CCPA), lock out "
    "every localized jurisdiction replacement, including a playbook forum swap."
)

_RAW_KEYS = frozenset(
    {
        "texto_original_puro",
        "original_blocks",
        "contract_text",
        "texto_contrato",
        "pages",
        "page_text",
        "chunks",
        "raw_chunks",
    }
)
_INTENTION_KEYS = (
    "normativas_detectadas",
    "intencion",
    "intention",
    "intencion_metadata",
    "metadata",
    "cumplimiento",
)
_PRIVACY_MARKS = ("gdpr", "ccpa", "rgpd")
_OUTPUT_KEYS = (
    "texto_reemplazo_word",
    "explicacion_comercial",
    "riesgos_originales",
    "advertencia_responsabilidad_abogado",
)
_LAWYER_WARNING = (
    "Este texto es material de apoyo para la revisión. No sustituye el criterio "
    "de un abogado licenciado. LexShield no emite opinión legal ni representa a las partes."
)


def generate_safe_alternative_clause(validated_finding: dict) -> dict[str, str]:
    """Build the Word payload from a verified finding, never from contract text."""
    finding = _published_finding(validated_finding)
    severity = _severity(finding)
    privacy_locked = _privacy_intention(finding)
    critical = severity == "CRÍTICO"
    cap = _cap_usd(validated_finding.get("mapa_definiciones"))
    replacement = _replacement(critical, privacy_locked, cap)
    return {
        "texto_reemplazo_word": replacement,
        "explicacion_comercial": _commercial(critical, privacy_locked, validated_finding),
        "riesgos_originales": _original_risks(finding, validated_finding, severity),
        "advertencia_responsabilidad_abogado": _LAWYER_WARNING,
    }


def _published_finding(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise TypeError("validated_finding must be the Week 4 verified payload.")
    if _contains_raw_chunk(payload):
        raise ValueError("Raw contract chunks are not accepted by the redactor.")
    if payload.get("verified") is not True:
        raise ValueError("Only a verified Week 4 payload can be redacted.")
    if payload.get("cola_revision_humana"):
        raise ValueError("A finding on cola_revision_humana cannot be redacted.")
    published = payload.get("salida_final")
    if (
        not isinstance(published, list)
        or len(published) != 1
        or not isinstance(published[0], dict)
    ):
        raise ValueError("The verified payload must contain one publishable finding.")
    if _contains_raw_chunk(published[0]):
        raise ValueError("Raw contract chunks are not accepted by the redactor.")
    return published[0]


def _contains_raw_chunk(value: object, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if isinstance(value, dict):
        return any(
            str(key) in _RAW_KEYS or _contains_raw_chunk(item, depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_raw_chunk(item, depth + 1) for item in value)
    return False


def _severity(finding: dict) -> str:
    for key in ("risk_tier", "severity", "severidad"):
        value = finding.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _privacy_intention(finding: dict) -> bool:
    """True when intention metadata flags GDPR, CCPA, or RGPD."""
    if finding.get("faltante_privacidad") is True:
        return False
    for key in _INTENTION_KEYS:
        if _mentions_global_privacy(finding.get(key)):
            return True
    return False


def _mentions_global_privacy(value: object) -> bool:
    if isinstance(value, str):
        folded = value.casefold()
        return any(mark in folded for mark in _PRIVACY_MARKS)
    if isinstance(value, list):
        return any(_mentions_global_privacy(item) for item in value)
    if isinstance(value, dict):
        return any(_mentions_global_privacy(item) for item in value.values())
    return False


def _cap_usd(definitions: object) -> float | None:
    if not isinstance(definitions, dict):
        return None
    amount = definitions.get("liability_cap_usd")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    return float(amount)


def _replacement(critical: bool, privacy_locked: bool, cap: float | None) -> str:
    if critical:
        if cap is None:
            fee_cap = (
                "un tope fijo igual a los honorarios efectivamente pagados durante "
                "los tres (3) meses anteriores al reclamo"
            )
        else:
            fee_cap = f"un tope fijo y cerrado de USD {cap:.2f}"
        text = (
            "Se elimina la fórmula de penalidad, incluidos los porcentajes sobre "
            "ingresos, el pool anual y cualquier liquidación sin tope. "
            f"La suma máxima exigible por este concepto será {fee_cap}, "
            "sin ampliación por ingresos brutos, daños indirectos ni penalidades abiertas."
        )
    else:
        text = (
            "La severidad calculada no autoriza a eliminar la penalidad ni a "
            "sustituirla por otra fórmula."
        )
    if privacy_locked:
        text += (
            " Este hallazgo está marcado como cumplimiento global de privacidad "
            "(GDPR/CCPA). Queda prohibido reemplazar la ley aplicable o el foro "
            "por una jurisdicción local o por la plantilla de foro del playbook."
        )
    return text


def _commercial(critical: bool, privacy_locked: bool, payload: dict) -> str:
    if critical:
        reason = (
            "La severidad calculada es CRÍTICO, así que la alternativa quita el "
            "diseño de la penalidad e impone un tope de honorarios cerrado."
        )
    else:
        reason = (
            "La severidad calculada no es CRÍTICO, así que no se propone "
            "eliminar la penalidad."
        )
    if payload.get("contradiccion_detectada") is True:
        reason += " La exposición calculada supera el tope definido en las cláusulas iniciales."
    if privacy_locked:
        reason += " Las reglas de sustitución de foro local quedan bloqueadas."
    return reason


def _original_risks(finding: dict, payload: dict, severity: str) -> str:
    exposure = finding.get("exposure_usd")
    exposure_text = "no calculada" if not isinstance(exposure, (int, float)) or isinstance(exposure, bool) else f"USD {float(exposure):.2f}"
    base = finding.get("base_calculo")
    base_text = base.strip() if isinstance(base, str) and base.strip() else "no declarada"
    penalty = finding.get("valor_multa")
    penalty_text = penalty.strip() if isinstance(penalty, str) and penalty.strip() else "no declarada"
    contradiction = "sí" if payload.get("contradiccion_detectada") is True else "no"
    tier = severity or "no calculada"
    return (
        f"Severidad calculada: {tier}. Exposición: {exposure_text}. "
        f"Penalidad declarada: {penalty_text}. Base declarada: {base_text}. "
        f"Contradicción con el tope inicial: {contradiction}."
    )


def _check() -> list[dict[str, str]]:
    critical = generate_safe_alternative_clause(
        {
            "verified": True,
            "contradiccion_detectada": True,
            "mapa_definiciones": {"liability_cap_usd": 50000, "clause_id": "1"},
            "salida_final": [
                {
                    "clause_id": "2",
                    "risk_tier": "CRÍTICO",
                    "exposure_usd": 60000,
                    "valor_multa": "60%",
                    "base_calculo": "annual pool",
                }
            ],
            "cola_revision_humana": [],
        }
    )
    privacy = generate_safe_alternative_clause(
        {
            "verified": True,
            "contradiccion_detectada": False,
            "mapa_definiciones": {"liability_cap_usd": None, "clause_id": None},
            "salida_final": [
                {
                    "clause_id": "4",
                    "risk_tier": "CRÍTICO",
                    "exposure_usd": 10000,
                    "normativas_detectadas": ["GDPR", "CCPA"],
                    "faltante_privacidad": False,
                    "intention": "global data privacy compliance",
                }
            ],
            "cola_revision_humana": [],
        }
    )
    moderate = generate_safe_alternative_clause(
        {
            "verified": True,
            "contradiccion_detectada": False,
            "salida_final": [{"clause_id": "5", "risk_tier": "MODERADO", "exposure_usd": 10000}],
            "cola_revision_humana": [],
        }
    )
    results = [critical, privacy, moderate]
    for result in results:
        if tuple(result) != _OUTPUT_KEYS:
            raise RuntimeError(f"output keys drifted: {tuple(result)!r}")
    if "Se elimina la fórmula de penalidad" not in critical["texto_reemplazo_word"]:
        raise RuntimeError("critical redraft did not remove the penalty layout")
    if "USD 50000.00" not in critical["texto_reemplazo_word"]:
        raise RuntimeError("critical redraft did not impose the verified fee cap")
    if "Costa Rica" in privacy["texto_reemplazo_word"] or "tribunales de" in privacy["texto_reemplazo_word"]:
        raise RuntimeError("privacy redraft applied a local forum")
    if "GDPR/CCPA" not in privacy["texto_reemplazo_word"]:
        raise RuntimeError("privacy lock was not written into the replacement")
    if "eliminar la penalidad" in moderate["texto_reemplazo_word"] and "no autoriza" not in moderate["texto_reemplazo_word"]:
        raise RuntimeError("non-critical redraft removed the penalty")
    for prompt_part in ("texto_original_puro", "CRÍTICO", "GDPR/CCPA", "localized jurisdiction"):
        if prompt_part not in ISOLATION_PROMPT:
            raise RuntimeError(f"isolation prompt lost its lock: {prompt_part}")
    try:
        generate_safe_alternative_clause({"verified": False, "salida_final": [], "cola_revision_humana": [{}]})
        raise RuntimeError("unverified payload was redacted")
    except ValueError:
        pass
    try:
        generate_safe_alternative_clause(
            {
                "verified": True,
                "salida_final": [{"texto_original_puro": "raw clause", "risk_tier": "CRÍTICO"}],
                "cola_revision_humana": [],
            }
        )
        raise RuntimeError("raw contract chunk was ingested")
    except ValueError:
        pass
    return results


if __name__ == "__main__":
    print(json.dumps(_check(), ensure_ascii=False, indent=2))
