"""Cross-page quotes anchor to the page where they start. Synthetic text only."""

from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from app.auditor import (
    ABSENT,
    _HEADING,
    _citation_context,
    _cross_page_start,
    _excerpt_in_contract,
    _excerpt_without_pagination,
    _map_collapsed,
    _pages,
    _publish,
)

_BAIT = "ZXQ-H2-BAIT-3391"


def _risk(risk_id: str, article: str, excerpt: str) -> dict[str, str]:
    return {
        "id": risk_id,
        "origin": "auditor",
        "article": article,
        "severity": "medium",
        "title": "Plazo distinto",
        "excerpt": excerpt,
        "analysis": "El calendario citado cruza el salto de pagina.",
        "suggestion": "Reescriba el calendario en una sola oracion.",
        "alternative_drafting": "El plazo queda descrito en una sola oracion continua.",
    }


def _payload(risks: list) -> dict:
    return {
        "summary": "Revision de un contrato ficticio.",
        "document_title": "Contrato ficticio",
        "metadata": {
            "parties": "Partes ficticias",
            "fees": "Sin cifra",
            "term": "Un mes",
            "contract_date": "4 de marzo de 2024",
        },
        "omissions": [],
        "risks": risks,
    }


def _with_bait(contract: str) -> str:
    return f"{contract}{_BAIT}\n"


def _page_hits(quote: str, contract: str) -> list[int]:
    return [
        number
        for number, body in _pages(contract)
        if _excerpt_in_contract(quote, body)
    ]


def _citation_context_before_cross_page(
    excerpt: str,
    contract_text: str,
    article: str,
) -> tuple[int, str]:
    """Single-page citation result from before the cross-page fallback."""
    page = 1
    for number, body in _pages(contract_text):
        if excerpt and _excerpt_in_contract(excerpt, body):
            page = number
            break
    section = article.strip() or "Cláusula citada"
    position = contract_text.find(excerpt) if excerpt else -1
    if position < 0 and excerpt:
        collapsed = " ".join(contract_text.split())
        collapsed_index = collapsed.find(excerpt)
        if collapsed_index >= 0:
            position = _map_collapsed(contract_text, collapsed_index)
    if position >= 0:
        heading = ""
        for match in _HEADING.finditer(contract_text):
            if match.start() > position:
                break
            heading = match.group(1)
        if heading:
            section = f"Art. {heading}"
    return page, section


class CrossPageAnchorTests(unittest.TestCase):
    def test_quote_crossing_from_page_2_is_accepted_on_page_2(self) -> None:
        contract = _with_bait(
            "[[PÁGINA 1]]\n"
            "CLÁUSULA PRIMERA.\n"
            "El archivo inicial solo describe el encargo de la primera hoja.\n"
            "[[PÁGINA 2]]\n"
            "CLÁUSULA SEGUNDA.\n"
            "El plazo convenido queda en treinta dias "
            "Página 2 de 3[[PÁGINA 3]]"
            "distinto del calendario ordinario.\n"
            "CLÁUSULA TERCERA.\n"
            "Cada parte conserva sus metodos de trabajo previos.\n"
        )
        quote = (
            "El plazo convenido queda en treinta dias "
            "distinto del calendario ordinario."
        )
        self.assertEqual(_page_hits(quote, contract), [])
        self.assertEqual(_cross_page_start(quote, contract), 2)
        risk = _risk("cross-from-2", "CLÁUSULA SEGUNDA", quote)
        output = _publish(copy.deepcopy(_payload([risk])), contract)
        kept = next((item for item in output["risks"] if item["id"] == "cross-from-2"), None)
        reasons = output["integrity"]["discard_reasons"]
        page = kept["pagina_exacta"] if kept else None
        context = kept["contexto_insercion_word"] if kept else None
        print(
            "CROSS_PAGE_ANCHOR_1 "
            f"kept={kept is not None} "
            f"pagina_exacta={page} "
            f"discard_reasons={reasons} "
            f"contexto_insercion_word={context}",
            flush=True,
        )
        self.assertIsNotNone(kept)
        self.assertEqual(page, 2)
        self.assertNotEqual(page, 1)
        self.assertNotEqual(page, 3)
        self.assertEqual(reasons["quote_not_anchored"], 0)
        self.assertEqual(reasons["failed_verification"], 0)
        self.assertEqual(reasons["malformed_item"], 0)
        self.assertEqual(context, ABSENT)
        self.assertIn(_BAIT, contract)
        self.assertNotIn(_BAIT, json.dumps(output["integrity"]))

    def test_quote_crossing_from_page_1_anchors_to_page_1(self) -> None:
        contract = _with_bait(
            "[[PÁGINA 1]]\n"
            "CLÁUSULA PRIMERA.\n"
            "El horario de archivo abre al amanecer "
            "Página 1 de 2[[PÁGINA 2]]"
            "y cierra cuando termina el turno.\n"
            "CLÁUSULA SEGUNDA.\n"
            "Cada parte conserva sus metodos de trabajo previos.\n"
        )
        quote = "El horario de archivo abre al amanecer y cierra cuando termina el turno."
        self.assertEqual(_page_hits(quote, contract), [])
        self.assertEqual(_cross_page_start(quote, contract), 1)
        output = _publish(
            copy.deepcopy(_payload([_risk("cross-from-1", "CLÁUSULA PRIMERA", quote)])),
            contract,
        )
        kept = next(item for item in output["risks"] if item["id"] == "cross-from-1")
        self.assertEqual(kept["pagina_exacta"], 1)
        self.assertEqual(output["integrity"]["discard_reasons"]["quote_not_anchored"], 0)

    def test_nonadjacent_clauses_are_rejected(self) -> None:
        contract = _with_bait(
            "[[PÁGINA 1]]\n"
            "CLÁUSULA PRIMERA.\n"
            "Fin visible de la clausula alfa en este punto.\n"
            "Página 1 de 2[[PÁGINA 2]]"
            "CLÁUSULA SEGUNDA.\n"
            "Inicio visible de la clausula beta en este punto.\n"
        )
        quote = (
            "Fin visible de la clausula alfa en este punto. "
            "Inicio visible de la clausula beta en este punto."
        )
        self.assertFalse(_excerpt_in_contract(quote, contract))
        self.assertIsNone(_cross_page_start(quote, contract))
        output = _publish(
            copy.deepcopy(_payload([_risk("joined-gap", "CLÁUSULA PRIMERA", quote)])),
            contract,
        )
        self.assertNotIn("joined-gap", [item["id"] for item in output["risks"]])
        self.assertGreater(output["integrity"]["discard_reasons"]["failed_verification"], 0)

    def test_one_changed_word_is_rejected(self) -> None:
        contract = _with_bait(
            "[[PÁGINA 2]]\n"
            "CLÁUSULA SEGUNDA.\n"
            "El plazo convenido queda en treinta dias "
            "Página 2 de 3[[PÁGINA 3]]"
            "distinto del calendario ordinario.\n"
        )
        quote = (
            "El plazo convenido queda en cuarenta dias "
            "distinto del calendario ordinario."
        )
        self.assertFalse(_excerpt_in_contract(quote, contract))
        self.assertIsNone(_cross_page_start(quote, contract))
        output = _publish(
            copy.deepcopy(_payload([_risk("changed-word", "CLÁUSULA SEGUNDA", quote)])),
            contract,
        )
        self.assertNotIn("changed-word", [item["id"] for item in output["risks"]])
        self.assertGreater(output["integrity"]["discard_reasons"]["failed_verification"], 0)

    def test_invented_and_reordered_quotes_are_rejected(self) -> None:
        contract = _with_bait(
            "[[PÁGINA 1]]\n"
            "CLÁUSULA PRIMERA.\n"
            "El calendario ordinario empieza en marzo y termina en agosto "
            "Página 1 de 2[[PÁGINA 2]]"
            "segun el anexo interno del encargo.\n"
        )
        invented = "Esta oracion fue inventada para no coincidir con el texto."
        reordered = "agosto en termina y marzo en empieza ordinario calendario El"
        for risk_id, quote in (("invented", invented), ("reordered", reordered)):
            self.assertFalse(_excerpt_in_contract(quote, contract))
            self.assertIsNone(_cross_page_start(quote, contract))
            output = _publish(
                copy.deepcopy(_payload([_risk(risk_id, "CLÁUSULA PRIMERA", quote)])),
                contract,
            )
            self.assertEqual(output["risks"], [])
            self.assertGreater(
                output["integrity"]["discard_reasons"]["failed_verification"],
                0,
            )

    def test_ambiguous_cross_page_sentence_is_rejected(self) -> None:
        contract = _with_bait(
            "[[PÁGINA 1]]\n"
            "CLÁUSULA PRIMERA.\n"
            "El puente unico cruza el rio antes del descanso "
            "Página 1 de 4[[PÁGINA 2]]"
            "y sigue al otro lado del valle.\n"
            "La senda exclusiva del mirador permanece abierta todo el ano "
            "Página 2 de 4[[PÁGINA 3]]"
            "sin cierre estacional alguno.\n"
            "CLÁUSULA TERCERA.\n"
            "El puente unico cruza el rio antes del descanso "
            "Página 3 de 4[[PÁGINA 4]]"
            "y sigue al otro lado del valle.\n"
        )
        ambiguous = (
            "El puente unico cruza el rio antes del descanso "
            "y sigue al otro lado del valle."
        )
        control = (
            "La senda exclusiva del mirador permanece abierta todo el ano "
            "sin cierre estacional alguno."
        )
        self.assertEqual(_page_hits(ambiguous, contract), [])
        self.assertEqual(_page_hits(control, contract), [])
        self.assertIsNone(_cross_page_start(ambiguous, contract))
        self.assertEqual(_cross_page_start(control, contract), 2)
        output = _publish(
            copy.deepcopy(
                _payload(
                    [
                        _risk("ambiguous-span", "CLÁUSULA PRIMERA", ambiguous),
                        _risk("unique-span", "CLÁUSULA TERCERA", control),
                    ]
                )
            ),
            contract,
        )
        kept_ids = [item["id"] for item in output["risks"]]
        self.assertNotIn("ambiguous-span", kept_ids)
        self.assertIn("unique-span", kept_ids)
        kept = next(item for item in output["risks"] if item["id"] == "unique-span")
        self.assertEqual(kept["pagina_exacta"], 2)
        self.assertEqual(output["integrity"]["discard_reasons"]["quote_not_anchored"], 1)
        self.assertEqual(output["integrity"]["discard_reasons"]["failed_verification"], 0)

    def test_artifact_only_or_short_cleaned_quote_is_rejected(self) -> None:
        contract = _with_bait(
            "[[PÁGINA 1]]\n"
            "CLÁUSULA PRIMERA.\n"
            "Texto de relleno de la hoja inicial del encargo ficticio.\n"
            "[[PÁGINA 2]]\n"
            "CLÁUSULA SEGUNDA.\n"
            "zz Página 2 de 3[[PÁGINA 3]]yy sigue el resto de la hoja.\n"
        )
        artifacts = "Página 2 de 3[[PÁGINA 3]]"
        short = "zz Página 2 de 3[[PÁGINA 3]]yy"
        self.assertEqual(" ".join(_excerpt_without_pagination(artifacts).split()), "")
        self.assertEqual(" ".join(_excerpt_without_pagination(short).split()), "zz yy")
        self.assertLess(len(" ".join(_excerpt_without_pagination(short).split())), 8)
        self.assertGreaterEqual(len(artifacts), 8)
        self.assertGreaterEqual(len(short), 8)
        self.assertTrue(_excerpt_in_contract(artifacts, contract))
        self.assertTrue(_excerpt_in_contract(short, contract))
        self.assertEqual(_page_hits(artifacts, contract), [])
        self.assertEqual(_page_hits(short, contract), [])
        self.assertIsNone(_cross_page_start(artifacts, contract))
        self.assertIsNone(_cross_page_start(short, contract))
        self.assertIsNone(_cross_page_start("abcdefg", "abcdefg en el texto largo."))
        output = _publish(
            copy.deepcopy(
                _payload(
                    [
                        _risk("artifacts-only", "CLÁUSULA SEGUNDA", artifacts),
                        _risk("short-cleaned", "CLÁUSULA SEGUNDA", short),
                    ]
                )
            ),
            contract,
        )
        self.assertEqual(output["risks"], [])
        reasons = output["integrity"]["discard_reasons"]
        self.assertEqual(reasons["failed_verification"], 0)
        self.assertEqual(reasons["quote_not_anchored"], 2)

    def test_single_page_quotes_keep_the_same_page_and_skip_cross_page(self) -> None:
        contract = _with_bait(
            "[[PÁGINA 1]]\n"
            "Cláusula 4. El texto de apertura queda archivado.\n"
            "[[PÁGINA 2]]\n"
            "Cláusula 9. El canon mensual se paga por mes vencido dentro del plazo.\n"
        )
        exact = "El canon mensual se paga por mes vencido"
        irregular = "El   canon\nmensual se paga por mes vencido"
        opening = "El texto de apertura queda archivado."
        cases = (
            (exact, "Cláusula 9"),
            (irregular, "Cláusula 9"),
            (opening, "Cláusula 4"),
        )
        for excerpt, article in cases:
            expected = _citation_context_before_cross_page(excerpt, contract, article)
            with patch("app.auditor._cross_page_start") as cross:
                actual = _citation_context(excerpt, contract, article)
                cross.assert_not_called()
            self.assertEqual(actual, expected)
            self.assertEqual(actual[0], 1 if article == "Cláusula 4" else 2)

        for risk_id, excerpt, article, page in (
            ("exact-page-2", exact, "Cláusula 9", 2),
            ("space-page-2", irregular, "Cláusula 9", 2),
            ("exact-page-1", opening, "Cláusula 4", 1),
        ):
            with patch("app.auditor._cross_page_start") as cross:
                output = _publish(
                    copy.deepcopy(_payload([_risk(risk_id, article, excerpt)])),
                    contract,
                )
                cross.assert_not_called()
            kept = next(item for item in output["risks"] if item["id"] == risk_id)
            self.assertEqual(kept["pagina_exacta"], page)
            self.assertEqual(output["integrity"]["discard_reasons"]["quote_not_anchored"], 0)
            self.assertEqual(output["integrity"]["discard_reasons"]["failed_verification"], 0)

    def test_typographic_difference_is_rejected_by_cross_page_path(self) -> None:
        contract = _with_bait(
            "[[PÁGINA 1]]\n"
            "CLÁUSULA PRIMERA.\n"
            "Texto de relleno de la hoja inicial sin la cita.\n"
            "[[PÁGINA 2]]\n"
            "CLÁUSULA SEGUNDA.\n"
            "El plazo \u201cfirme\u201d queda "
            "Página 2 de 3[[PÁGINA 3]]"
            "fijado\u2014salvo pacto en treinta dias.\n"
        )
        quote = 'El plazo "firme" queda fijado-salvo pacto en treinta dias.'
        same_marks = (
            "El plazo \u201cfirme\u201d queda fijado\u2014salvo pacto en treinta dias."
        )
        self.assertTrue(_excerpt_in_contract(quote, contract))
        self.assertEqual(_page_hits(quote, contract), [])
        self.assertIsNone(_cross_page_start(quote, contract))
        self.assertEqual(_cross_page_start(same_marks, contract), 2)
        output = _publish(
            copy.deepcopy(_payload([_risk("typo-span", "CLÁUSULA SEGUNDA", quote)])),
            contract,
        )
        reasons = output["integrity"]["discard_reasons"]
        print(
            "CROSS_PAGE_TYPOGRAPHY "
            f"kept={any(item['id'] == 'typo-span' for item in output['risks'])} "
            f"cross_page_start=None "
            f"same_punctuation_page=2 "
            f"quote_not_anchored={reasons['quote_not_anchored']} "
            f"failed_verification={reasons['failed_verification']}",
            flush=True,
        )
        self.assertEqual(output["risks"], [])
        self.assertEqual(reasons["failed_verification"], 0)
        self.assertEqual(reasons["quote_not_anchored"], 1)

    def test_cross_page_start_direct_cases(self) -> None:
        single = (
            "[[PÁGINA 1]]\n"
            "Hoja inicial sin la frase buscada.\n"
            "[[PÁGINA 5]]\n"
            "La frase unica empieza aqui "
            "Página 5 de 6[[PÁGINA 6]]"
            "y termina despues del salto.\n"
        )
        single_quote = "La frase unica empieza aqui y termina despues del salto."
        self.assertEqual(_cross_page_start(single_quote, single), 5)
        self.assertEqual(
            _cross_page_start(
                "La   frase unica\nempieza aqui y termina despues del salto.",
                single,
            ),
            5,
        )
        after_marker = (
            "[[PÁGINA 1]]\n"
            "texto previo de la primera hoja.\n"
            "[[PÁGINA 7]]inmediatamente despues del marcador sigue la frase."
        )
        self.assertEqual(
            _cross_page_start("inmediatamente despues del marcador sigue", after_marker),
            7,
        )

        same_page = (
            "[[PÁGINA 4]]\n"
            "alfa beta gamma delta uno. alfa beta gamma delta uno.\n"
        )
        repeated = "alfa beta gamma delta uno."
        self.assertEqual(_cross_page_start(repeated, same_page), 4)
        different_pages = (
            "[[PÁGINA 1]]\n"
            "alfa beta gamma delta uno.\n"
            "[[PÁGINA 2]]\n"
            "alfa beta gamma delta uno.\n"
        )
        self.assertIsNone(_cross_page_start(repeated, different_pages))
        unmarked_repeat = "alfa beta gamma delta uno. alfa beta gamma delta uno."
        self.assertEqual(_cross_page_start(repeated, unmarked_repeat), 1)

        unmarked = "El contrato completo vive en una sola pieza continua."
        self.assertEqual(
            _cross_page_start("vive en una sola pieza continua.", unmarked),
            1,
        )

        preamble = (
            "Texto inicial sin marca previa. "
            "[[PÁGINA 2]]\n"
            "resto de la pagina marcada.\n"
        )
        self.assertIsNone(_cross_page_start("Texto inicial sin marca previa.", preamble))
        mixed = (
            "Texto inicial sin marca previa. "
            "[[PÁGINA 2]]\n"
            "Texto inicial sin marca previa.\n"
        )
        self.assertIsNone(_cross_page_start("Texto inicial sin marca previa.", mixed))
        self.assertIsNone(_cross_page_start("frase ausente del texto", single))


if __name__ == "__main__":
    unittest.main()
