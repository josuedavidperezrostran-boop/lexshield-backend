"""Integrity status is additive and does not change published findings."""

from __future__ import annotations

import copy
import json
import re
import unittest

from app.auditor import ABSENT, _publish, apply_truncation_to_integrity

_SIX_CLAUSES = (
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
_NEUTRAL_SUMMARY = "Revision de un contrato ficticio de seis clausulas."
_DATED = "4 de marzo de 2024"
_COMPLETE_MESSAGE = (
    "Los hallazgos publicados tienen cita textual verificada en el contrato. "
    "Este informe es apoyo a la decisión y requiere revisión por un abogado."
)
_INCOMPLETE_MESSAGE = (
    "Algunos hallazgos no pudieron verificarse o el análisis puede ser parcial. "
    "Revisión humana obligatoria."
)
_UNRELIABLE_MESSAGE = (
    "No fue posible verificar hallazgos. "
    "No interprete esto como ausencia de riesgos. Revisión humana obligatoria."
)
_BAIT_STRING = "ZXQ-BAIT-STRING-7741"
_BAIT_EMAIL = "bait.privacidad@example.test"
_BAIT_FILENAME = "contrato-bait-7741.pdf"
_GENERATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _finding(
    risk_id: str,
    article: str,
    excerpt: str,
    title: str = "Pago por adelantado",
    analysis: str = "El anticipo traslada el riesgo de caja al cliente.",
) -> dict[str, str]:
    return {
        "id": risk_id,
        "origin": "auditor",
        "article": article,
        "severity": "medium",
        "title": title,
        "excerpt": excerpt,
        "analysis": analysis,
        "suggestion": "Condicione el anticipo a una entrega aceptada.",
        "alternative_drafting": "El pago se devenga contra entrega aceptada.",
    }


def _payload(
    risks: list,
    summary: str = _NEUTRAL_SUMMARY,
    contract_date: str | None = _DATED,
) -> dict:
    metadata: dict[str, str] = {"parties": "Partes ficticias", "fees": "Sin cifra", "term": "Un mes"}
    if contract_date is not None:
        metadata["contract_date"] = contract_date
    return {
        "summary": summary,
        "document_title": "Contrato ficticio",
        "metadata": metadata,
        "omissions": [],
        "risks": risks,
    }


def _valid_risk(risk_id: str = "valid-ordinal") -> dict[str, str]:
    return _finding(risk_id, "CLÁUSULA PRIMERA", "El pago se realiza por adelantado")


class IntegrityStatusTests(unittest.TestCase):
    def _assert_shape(self, integrity: dict) -> None:
        self.assertEqual(
            set(integrity),
            {
                "version",
                "status",
                "status_label",
                "message",
                "total_candidates",
                "verified",
                "discarded",
                "added_by_controls",
                "discard_reasons",
                "flags",
                "notes",
                "truncated",
                "generated_at",
            },
        )
        self.assertEqual(integrity["version"], "integrity-v1")
        self.assertEqual(
            set(integrity["discard_reasons"]),
            {"malformed_item", "failed_verification", "quote_not_anchored"},
        )
        self.assertRegex(integrity["generated_at"], _GENERATED_AT)
        self.assertIsInstance(integrity["flags"], list)
        self.assertIsInstance(integrity["notes"], list)

    def test_complete_when_every_quote_is_literal(self) -> None:
        output = _publish(_payload([_valid_risk()]), _SIX_CLAUSES)
        integrity = output["integrity"]
        self._assert_shape(integrity)
        self.assertEqual(integrity["status"], "complete")
        self.assertEqual(integrity["status_label"], "Análisis completo")
        self.assertEqual(integrity["message"], _COMPLETE_MESSAGE)
        self.assertEqual(integrity["discarded"], 0)
        self.assertEqual(integrity["verified"], 1)
        self.assertEqual(integrity["total_candidates"], 1)
        self.assertEqual(integrity["added_by_controls"], 0)
        self.assertEqual(integrity["flags"], [])
        self.assertEqual(integrity["notes"], [])
        self.assertIs(integrity["truncated"], False)
        self.assertEqual([risk["id"] for risk in output["risks"]], ["valid-ordinal"])

    def test_incomplete_when_one_excerpt_is_fabricated(self) -> None:
        risks = [
            _valid_risk(),
            _finding(
                "fabricated-ordinal",
                "CLÁUSULA SEGUNDA",
                "esta obligacion fue inventada por completo",
                title="Cita fabricada",
                analysis="La cita no pertenece al contrato.",
            ),
        ]
        output = _publish(_payload(risks), _SIX_CLAUSES)
        integrity = output["integrity"]
        self.assertEqual(integrity["status"], "incomplete")
        self.assertEqual(integrity["status_label"], "Análisis incompleto")
        self.assertEqual(integrity["message"], _INCOMPLETE_MESSAGE)
        self.assertEqual(integrity["discarded"], 1)
        self.assertEqual(integrity["discard_reasons"]["failed_verification"], 1)
        self.assertEqual(integrity["discard_reasons"]["quote_not_anchored"], 0)
        self.assertEqual(integrity["discard_reasons"]["malformed_item"], 0)
        self.assertEqual(integrity["verified"], 1)
        self.assertEqual([risk["id"] for risk in output["risks"]], ["valid-ordinal"])

    def test_unreliable_when_every_excerpt_is_fabricated(self) -> None:
        risks = [
            _finding(
                "fabricated-only",
                "CLÁUSULA SEGUNDA",
                "esta obligacion fue inventada por completo",
                title="Cita fabricada",
                analysis="La cita no pertenece al contrato.",
            )
        ]
        output = _publish(_payload(risks), _SIX_CLAUSES)
        integrity = output["integrity"]
        self.assertEqual(integrity["status"], "unreliable")
        self.assertEqual(integrity["status_label"], "Sin resultados fiables")
        self.assertEqual(integrity["message"], _UNRELIABLE_MESSAGE)
        self.assertEqual(integrity["verified"], 0)
        self.assertEqual(output["risks"], [])

    def test_unreliable_when_the_risk_list_is_empty(self) -> None:
        output = _publish(_payload([]), "Acuerdo ficticio sin clausulas ni cifras.\n")
        integrity = output["integrity"]
        self.assertEqual(integrity["status"], "unreliable")
        self.assertEqual(integrity["verified"], 0)
        self.assertEqual(integrity["total_candidates"], 0)
        self.assertEqual(integrity["discarded"], 0)
        self.assertIn("No interprete esto como ausencia de riesgos", integrity["message"])

    def test_summary_unsupported_flags_without_changing_findings(self) -> None:
        summary = (
            "El dictamen nombra propiedad intelectual, dolo, jurisdicción e interés "
            "sin ficha que los sostenga."
        )
        flagged = _publish(_payload([_valid_risk()], summary=summary), _SIX_CLAUSES)
        neutral = _publish(_payload([_valid_risk()]), _SIX_CLAUSES)
        integrity = flagged["integrity"]
        self.assertIn("summary_unsupported", integrity["flags"])
        self.assertEqual(integrity["status"], "incomplete")
        self.assertEqual(integrity["status_label"], "Análisis incompleto")
        self.assertEqual(integrity["message"], _INCOMPLETE_MESSAGE)
        self.assertEqual(integrity["discarded"], 0)
        self.assertNotIn("summary_unsupported", neutral["integrity"]["flags"])
        self.assertEqual(neutral["integrity"]["status"], "complete")
        self.assertEqual(
            [risk["id"] for risk in flagged["risks"]],
            [risk["id"] for risk in neutral["risks"]],
        )
        self.assertEqual(len(flagged["risks"]), len(neutral["risks"]))
        self.assertEqual(flagged["risks"], neutral["risks"])

    def test_truncation_downgrades_only_a_complete_audit(self) -> None:
        complete = _publish(_payload([_valid_risk()]), _SIX_CLAUSES)
        untouched = copy.deepcopy(complete)
        apply_truncation_to_integrity(untouched, False)
        self.assertEqual(untouched, complete)

        downgraded = copy.deepcopy(complete)
        apply_truncation_to_integrity(downgraded, True)
        self.assertEqual(downgraded["integrity"]["status"], "incomplete")
        self.assertEqual(downgraded["integrity"]["status_label"], "Análisis incompleto")
        self.assertEqual(downgraded["integrity"]["message"], _INCOMPLETE_MESSAGE)
        self.assertIn("contract_truncated", downgraded["integrity"]["flags"])
        self.assertIs(downgraded["integrity"]["truncated"], True)
        self.assertEqual(downgraded["risks"], complete["risks"])
        apply_truncation_to_integrity(downgraded, True)
        self.assertEqual(downgraded["integrity"]["flags"].count("contract_truncated"), 1)

        unreliable = _publish(_payload([]), "Acuerdo ficticio sin clausulas ni cifras.\n")
        before = copy.deepcopy(unreliable["integrity"])
        apply_truncation_to_integrity(unreliable, True)
        self.assertEqual(unreliable["integrity"]["status"], "unreliable")
        self.assertEqual(unreliable["integrity"]["status_label"], before["status_label"])
        self.assertEqual(unreliable["integrity"]["message"], before["message"])
        self.assertIs(unreliable["integrity"]["truncated"], True)
        self.assertIn("contract_truncated", unreliable["integrity"]["flags"])

    def test_existing_audit_keys_remain_and_json_dumps(self) -> None:
        finding = _valid_risk()
        output = _publish(_payload([finding]), _SIX_CLAUSES)
        for key in ("summary", "document_title", "metadata", "omissions", "risks"):
            self.assertIn(key, output)
        self.assertIsInstance(output["metadata"], dict)
        self.assertIsInstance(output["omissions"], list)
        self.assertIsInstance(output["risks"], list)
        self.assertEqual(len(output["risks"]), 1)
        self.assertTrue(set(finding) <= set(output["risks"][0]))
        encoded = json.dumps(output)
        self.assertIsInstance(encoded, str)
        self.assertIn("valid-ordinal", encoded)

    def test_integrity_does_not_echo_contract_title_or_email(self) -> None:
        contract = (
            f"{_SIX_CLAUSES}"
            f"Referencia interna {_BAIT_STRING} {_BAIT_EMAIL} archivo {_BAIT_FILENAME}.\n"
        )
        title = f"Pago por adelantado {_BAIT_STRING} {_BAIT_EMAIL} {_BAIT_FILENAME}"
        finding = _finding("valid-ordinal", "CLÁUSULA PRIMERA", "El pago se realiza por adelantado", title=title)
        output = _publish(_payload([finding]), contract)
        encoded = json.dumps(output["integrity"])
        for bait in (_BAIT_STRING, _BAIT_EMAIL, _BAIT_FILENAME):
            self.assertIn(bait, contract)
            self.assertIn(bait, output["risks"][0]["title"])
            self.assertNotIn(bait, encoded)

    def test_missing_contract_date_is_a_note_and_keeps_complete(self) -> None:
        output = _publish(_payload([_valid_risk()], contract_date=None), _SIX_CLAUSES)
        integrity = output["integrity"]
        self.assertEqual(output["metadata"]["contract_date"], ABSENT)
        self.assertIn("contract_date_missing", integrity["notes"])
        self.assertNotIn("contract_date_missing", integrity["flags"])
        self.assertEqual(integrity["status"], "complete")
        self.assertEqual(integrity["status_label"], "Análisis completo")

    def test_malformed_item_counts_as_discarded(self) -> None:
        output = _publish(_payload([_valid_risk(), "no-es-una-ficha"]), _SIX_CLAUSES)
        integrity = output["integrity"]
        self.assertEqual(integrity["discard_reasons"]["malformed_item"], 1)
        self.assertEqual(integrity["discarded"], 1)
        self.assertEqual(integrity["total_candidates"], 2)
        self.assertEqual(integrity["status"], "incomplete")
        self.assertEqual([risk["id"] for risk in output["risks"]], ["valid-ordinal"])


if __name__ == "__main__":
    unittest.main()
