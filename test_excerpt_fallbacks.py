"""Fallback comparisons inside _excerpt_in_contract. Synthetic text only."""

from __future__ import annotations

import copy
import unittest

from app.auditor import (
    _citation_context,
    _excerpt_in_contract,
    _ground_risks,
    _pages,
    _publish,
    _risk_is_grounded,
)


def _risk(article: str, excerpt: str, risk_id: str = "clause-2-span") -> dict[str, str]:
    return {
        "id": risk_id,
        "origin": "auditor",
        "article": article,
        "severity": "medium",
        "title": "Plazo distinto",
        "excerpt": excerpt,
        "analysis": "El calendario citado omite un tramo del texto extraido.",
        "suggestion": "Reescriba el calendario en una sola oracion.",
        "alternative_drafting": "El plazo queda descrito en una sola oracion continua.",
    }


class ExcerptFallbackTests(unittest.TestCase):
    def test_a_pagination_artifact_is_skipped_only_when_words_match(self) -> None:
        contract = (
            "Texto previo del encargo. El plazo\n"
            "Página 1 de 2[[PÁGINA 2]]distinto sigue en la misma oracion."
        )
        quote = "El plazo distinto sigue"
        changed = "El plazo diferente sigue"
        self.assertTrue(_excerpt_in_contract(quote, contract))
        self.assertFalse(_excerpt_in_contract(changed, contract))
        self.assertEqual(
            contract,
            "Texto previo del encargo. El plazo\n"
            "Página 1 de 2[[PÁGINA 2]]distinto sigue en la misma oracion.",
        )
        self.assertEqual(quote, "El plazo distinto sigue")

        lowered = (
            "El plazo\n"
            "página 1 de 2[[página 2]]distinto sigue en la misma oracion."
        )
        self.assertTrue(_excerpt_in_contract(quote, lowered))

    def test_b_typographic_signs_unify_without_touching_words(self) -> None:
        contract = (
            "El plazo \u201cespecial\u201d\u00a0queda fijado\u2014salvo pacto\u2026 en treinta dias."
        )
        quote = 'El plazo "especial" queda fijado-salvo pacto... en treinta dias.'
        self.assertNotIn(quote, contract)
        self.assertNotIn(" ".join(quote.split()), " ".join(contract.split()))
        self.assertTrue(_excerpt_in_contract(quote, contract))

        singles = "Dice \u2018alfa\u2019 y \u00abbeta\u00bb en la nota."
        self.assertTrue(_excerpt_in_contract("Dice 'alfa' y beta en la nota.", singles))

        dashed = "uno\u2010dos\u2013tres\u2014cuatro\u2015cinco\u2212seis"
        self.assertTrue(_excerpt_in_contract("uno-dos-tres-cuatro-cinco-seis", dashed))

        spaced = "Palabra\u200bcompuesta\u2009siguiente\u202fsigno\u200a final\ufeff."
        self.assertTrue(
            _excerpt_in_contract("Palabracompuesta siguiente signo final.", spaced)
        )

    def test_c_one_changed_word_is_rejected(self) -> None:
        contract = (
            "El plazo \u201cespecial\u201d\u00a0queda fijado\u2014salvo pacto\u2026 en treinta dias."
        )
        quote = 'El plazo "especial" queda fijado-salvo pacto... en cuarenta dias.'
        self.assertFalse(_excerpt_in_contract(quote, contract))

    def test_d_invented_and_reordered_quotes_are_rejected(self) -> None:
        contract = "El calendario ordinario empieza en marzo y termina en agosto."
        invented = "Esta oracion fue inventada para no coincidir con el texto."
        reordered = "agosto en termina y marzo en empieza ordinario calendario El"
        self.assertFalse(_excerpt_in_contract(invented, contract))
        self.assertFalse(_excerpt_in_contract(reordered, contract))

    def test_e_noncontiguous_join_is_rejected(self) -> None:
        contract = (
            "Fin visible de la clausula alfa en este punto. "
            "Puente ajeno entre ambas clausulas del contrato. "
            "Inicio visible de la clausula beta en este punto."
        )
        joined = (
            "Fin visible de la clausula alfa en este punto. "
            "Inicio visible de la clausula beta en este punto."
        )
        self.assertFalse(_excerpt_in_contract(joined, contract))

        with_artifact = (
            "La primera clausula cierra el encargo de la alfa con firma. "
            "Texto intermedio que separa las dos clausulas de forma real. "
            "Página 1 de 2[[PÁGINA 2]]"
            "La segunda clausula abre el encargo de la beta con sello."
        )
        across = (
            "cierra el encargo de la alfa con firma. "
            "La segunda clausula abre el encargo de la beta"
        )
        self.assertFalse(_excerpt_in_contract(across, with_artifact))

    def test_f_comma_period_accent_and_case_stay_significant(self) -> None:
        contract = (
            "El relator declara que no, procede la medida solicitada por la parte. "
            "Mas adelante el texto dice que no procede la prorroga del calendario. "
            "Se documenta el cierre. inmediato de la operacion y nada mas. "
            "La rescisión del acuerdo figura en el anexo interno. "
            "El termino inicial permanece vigente durante el encargo."
        )
        self.assertTrue(_excerpt_in_contract("no, procede la medida", contract))
        self.assertTrue(_excerpt_in_contract("no procede la prorroga", contract))
        self.assertFalse(
            _excerpt_in_contract(
                "El relator declara que no procede la medida solicitada",
                contract,
            )
        )
        self.assertFalse(
            _excerpt_in_contract(
                "Se documenta el cierre inmediato de la operacion",
                contract,
            )
        )
        self.assertFalse(
            _excerpt_in_contract("La rescision del acuerdo figura", contract)
        )
        self.assertFalse(
            _excerpt_in_contract("El TERMINO inicial permanece vigente", contract)
        )

    def test_g_publish_reports_stage_for_clause_spanning_artifact(self) -> None:
        contract = (
            "[[PÁGINA 1]]\n"
            "CLÁUSULA PRIMERA.\n"
            "El pago se realiza por adelantado y sin deposito en garantia.\n"
            "CLÁUSULA SEGUNDA.\n"
            "El plazo convenido queda en treinta dias "
            "Página 1 de 2[[PÁGINA 2]]"
            "distinto del calendario ordinario.\n"
            "CLÁUSULA TERCERA.\n"
            "Cada parte conserva sus metodos de trabajo previos.\n"
        )
        excerpt = (
            "El plazo convenido queda en treinta dias "
            "distinto del calendario ordinario."
        )
        risk = _risk("CLÁUSULA SEGUNDA", excerpt)
        payload = {
            "summary": "Revision de un contrato ficticio de tres clausulas.",
            "document_title": "Contrato ficticio",
            "metadata": {
                "parties": "Partes ficticias",
                "fees": "Sin cifra",
                "term": "Un mes",
                "contract_date": "4 de marzo de 2024",
            },
            "omissions": [],
            "risks": [risk],
        }
        self.assertTrue(_excerpt_in_contract(excerpt, contract))
        grounded = _risk_is_grounded(copy.deepcopy(risk), contract)
        after_ground = _ground_risks([copy.deepcopy(risk)], contract)
        page, _section = _citation_context(excerpt, contract, risk["article"])
        page_hits = [
            number
            for number, body in _pages(contract)
            if _excerpt_in_contract(excerpt, body)
        ]
        prefix_len = len(excerpt)
        while prefix_len > 0 and excerpt[:prefix_len] not in contract:
            prefix_len -= 1
        quote_start = contract.find(excerpt[:prefix_len]) if prefix_len else -1
        start_page = None
        search_from = 0
        for number, body in _pages(contract):
            body_at = contract.find(body, search_from)
            if body_at < 0:
                continue
            if quote_start >= 0 and body_at <= quote_start:
                start_page = number
            search_from = body_at + 1
        if not grounded or not after_ground:
            stage = "_ground_risks"
            why = "ordinal_or_desk_scope_rejected"
        elif not page_hits:
            stage = "cross_page_anchored"
            why = (
                "quote_starts_on_page "
                f"citation_page={page} start_page={start_page} page_hits=[]"
            )
        else:
            stage = "anchored"
            why = f"page_hits={page_hits}"

        output = _publish(copy.deepcopy(payload), contract)
        integrity = output["integrity"]
        reasons = integrity["discard_reasons"]
        kept_ids = [item["id"] for item in output["risks"]]
        kept_card = next((item for item in output["risks"] if item["id"] == "clause-2-span"), None)
        kept = kept_card is not None
        pagina = kept_card["pagina_exacta"] if kept_card else None
        print(
            "EXCERPT_FALLBACK_G "
            f"kept={kept} "
            f"kept_ids={kept_ids} "
            f"stage={stage} "
            f"why={why} "
            f"pagina_exacta={pagina} "
            f"start_page={start_page} "
            f"discard_reasons={reasons} "
            f"status={integrity['status']} "
            f"verified={integrity['verified']} "
            f"discarded={integrity['discarded']}",
            flush=True,
        )
        self.assertEqual(
            set(reasons),
            {"malformed_item", "failed_verification", "quote_not_anchored"},
        )
        self.assertTrue(kept)
        self.assertEqual(reasons["quote_not_anchored"], 0)
        self.assertEqual(pagina, start_page)
        self.assertNotEqual(integrity["status"], "unreliable")

    def test_combined_artifact_and_typography(self) -> None:
        contract = (
            "El plazo \u201cfirme\u201d\n"
            "Página 1 de 2[[PÁGINA 2]]queda\u2014fijado en treinta dias."
        )
        quote = 'El plazo "firme" queda-fijado en treinta dias.'
        pages_only_contract = " ".join(
            contract.replace("Página 1 de 2", " ").replace("[[PÁGINA 2]]", " ").split()
        )
        self.assertNotIn(" ".join(quote.split()), pages_only_contract)
        self.assertTrue(_excerpt_in_contract(quote, contract))

    def test_h_exact_and_whitespace_behavior_is_unchanged(self) -> None:
        contract = "El canon mensual se paga por mes vencido dentro del plazo."
        self.assertTrue(
            _excerpt_in_contract("El canon mensual se paga por mes vencido", contract)
        )
        self.assertTrue(
            _excerpt_in_contract(
                "El   canon\nmensual se paga por mes vencido",
                contract,
            )
        )
        self.assertTrue(_excerpt_in_contract(contract, contract))
        self.assertFalse(
            _excerpt_in_contract("El canon anual se paga por mes vencido", contract)
        )
        self.assertFalse(_excerpt_in_contract("[[PÁGINA 9]]", contract))
        self.assertFalse(_excerpt_in_contract('""""', "Texto sin comillas dobles."))


if __name__ == "__main__":
    unittest.main()
