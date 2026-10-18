"""Ordinal-word clause headings stay grounded when the quote is real."""

from __future__ import annotations

from app.auditor import _publish, _risk_is_grounded


def _risk(article: str, excerpt: str, risk_id: str = "r1") -> dict[str, str]:
    return {"id": risk_id, "article": article, "excerpt": excerpt}


def _assert_kept(article: str, excerpt: str, contract: str) -> None:
    assert _risk_is_grounded(_risk(article, excerpt), contract), article


def _assert_dropped(article: str, excerpt: str, contract: str) -> None:
    assert not _risk_is_grounded(_risk(article, excerpt), contract), article


def main() -> None:
    primera = (
        "CLÁUSULA PRIMERA.\n"
        "El pago se realiza por adelantado y sin deposito en garantia.\n"
    )
    quote = "El pago se realiza por adelantado"
    _assert_kept("CLÁUSULA PRIMERA", quote, primera)
    _assert_kept("clausula primera", quote, primera)

    primero = "ARTÍCULO PRIMERO.\nEl informe se entrega en el domicilio del cliente.\n"
    _assert_kept("ARTÍCULO PRIMERO", "El informe se entrega en el domicilio", primero)
    unica = "CLÁUSULA ÚNICA.\nEste acuerdo ficticio no crea una sociedad entre las partes.\n"
    _assert_kept("CLÁUSULA ÚNICA", "no crea una sociedad entre las partes", unica)

    numbered = "Clausula 1.\nEl canon mensual se paga por mes vencido.\n"
    _assert_kept("Clausula 1", "El canon mensual se paga por mes vencido", numbered)
    _assert_dropped("Clausula 1", "esta frase no figura en el clausulado", numbered)

    prose = "El cliente abonara la primera cuota al inicio del mes convenido.\n"
    _assert_dropped("Primera", "la primera cuota al inicio", prose)

    _assert_dropped("Cláusula Decimotercera", quote, primera)
    fused = "CLÁUSULA DECIMOTERCERA.\n" + quote + " y queda documentado.\n"
    _assert_kept("Cláusula Decimotercera", quote, fused)

    decima = (
        "CLÁUSULA DÉCIMA SEGUNDA.\n"
        "Las comunicaciones se envian por correo certificado al domicilio.\n"
    )
    certified = "Las comunicaciones se envian por correo certificado"
    _assert_dropped("Décima Primera", certified, decima)
    _assert_kept("Décima Segunda", certified, decima)

    _assert_dropped("CLÁUSULA PRIMERA", "obligacion inventada por completo", primera)
    _assert_dropped("", quote, primera)

    contract = (
        "[[PÁGINA 1]]\n"
        "CLÁUSULA PRIMERA.\n"
        "El pago se realiza por adelantado y sin deposito en garantia.\n"
        "CLÁUSULA SEGUNDA.\n"
        "La entrega del informe ocurre en el domicilio del cliente.\n"
        "CLÁUSULA TERCERA.\n"
        "Cada parte conserva sus metodos de trabajo previos.\n"
        "CLÁUSULA CUARTA.\n"
        "Las comunicaciones se envian por correo certificado.\n"
        "CLÁUSULA QUINTA.\n"
        "Cualquiera puede solicitar una reunion de seguimiento.\n"
        "CLÁUSULA SEXTA.\n"
        "Este acuerdo ficticio no crea una sociedad entre las partes.\n"
    )
    payload = {
        "summary": "Revision de un contrato ficticio de seis clausulas.",
        "document_title": "Contrato ficticio",
        "metadata": {},
        "omissions": [],
        "risks": [
            {
                "id": "valid-ordinal",
                "origin": "auditor",
                "article": "CLÁUSULA PRIMERA",
                "severity": "high",
                "title": "Pago por adelantado",
                "excerpt": "El pago se realiza por adelantado",
                "analysis": "El anticipo traslada el riesgo de caja al cliente.",
                "suggestion": "Condicione el anticipo a una entrega aceptada.",
                "alternative_drafting": "El pago se devenga contra entrega aceptada.",
            },
            {
                "id": "fabricated-ordinal",
                "origin": "auditor",
                "article": "CLÁUSULA SEGUNDA",
                "severity": "medium",
                "title": "Cita fabricada",
                "excerpt": "esta obligacion fue inventada por completo",
                "analysis": "La cita no pertenece al contrato.",
                "suggestion": "Descarte el hallazgo.",
                "alternative_drafting": "Sin cambio al texto.",
            },
        ],
    }
    result = _publish(payload, contract)
    kept_ids = [risk["id"] for risk in result["risks"]]
    print(f"publish_ids={kept_ids}", flush=True)
    assert "valid-ordinal" in kept_ids
    assert "fabricated-ordinal" not in kept_ids
    print("ordinal grounding ok", flush=True)


if __name__ == "__main__":
    main()
