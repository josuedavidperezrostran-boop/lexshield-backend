from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Literal, NamedTuple

from fastapi import HTTPException
from openai import APIStatusError, OpenAI, RateLimitError
from pydantic import BaseModel, Field

from .config import settings
from .playbook import (
    acceptable_liability_percent,
    jurisdiction_violations,
    liability_violations,
    load_playbook,
)

logger = logging.getLogger("lexshield.auditor")

ABSENT = "No especificado"
_ARTICLE_NUM = re.compile(r"\d+(?:\.\d+)+|\d+")


def scan_financial_and_ip(
    contract_text: str,
    filename: str,
    reference_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mesa del auditor: pasivos, penas y transferencias de propiedad intelectual."""
    client = _client()
    user = _contract_message(
        filename,
        contract_text,
        "Lea el documento completo en un solo pase, de la primera página a la última, sin omitir el cierre. "
        "Detecte intereses o penas indexados a métricas volátiles o a fórmulas de capitalización, "
        "transmisiones de activos o participaciones a precio simbólico o a valor en libros bajo fuerza mayor, "
        "y plazos de cumplimiento inferiores a cinco días hábiles unidos a daños liquidados de gran cuantía. "
        "Cada hallazgo es una ficha propia. Copie solo cifras que el texto escriba. "
        "Si una variable no está especificada, escriba exactamente \"No especificado\" y no la calcule."
        f"{_map_note(reference_map)}"
    )
    return _agent(client, "lexshield_auditor", AUDITOR_SCHEMA, _auditor_prompt(), user)


def scan_jurisdiction(
    contract_text: str,
    filename: str,
    reference_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mesa del litigador: índice cronológico y contradicciones de foro."""
    client = _client()
    user = _contract_message(
        filename,
        contract_text,
        "Antes de informar contradicciones, liste en orden cronológico cada artículo, definición y subinciso. "
        "Compare el foro y la ley de las definiciones iniciales con las cláusulas de ejecución del final. "
        "Informe solo sedes, fueros o leyes que no puedan cumplirse a la vez, citando ambos artículos."
        f"{_map_note(reference_map)}"
    )
    return _agent(client, "lexshield_litigador", LITIGATOR_SCHEMA, _litigator_prompt(), user)


def compile_partner_opinion(
    contract_text: str,
    filename: str,
    auditor_notes: dict[str, Any],
    litigator_notes: dict[str, Any],
    reference_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mesa del socio: depura alucinaciones y publica el dictamen."""
    client = _client()
    user = (
        f"Filename: {filename}\n\n"
        "Contrato (efímero, no retener):\n\n"
        f"{contract_text}\n\n"
        "Memorando del auditor (pasivos, valoración, plazos, bloqueos y propiedad intelectual):\n"
        f"{json.dumps(auditor_notes, ensure_ascii=False)}\n\n"
        "Memorando del litigador (índice cronológico y contradicciones de foro):\n"
        f"{json.dumps(litigator_notes, ensure_ascii=False)}\n\n"
        "Conserve un hallazgo solo si el excerpt es cita literal del contrato y el número de artículo aparece en el texto. "
        "Si la cita no aparece, vuelva a leer esa página y las contiguas y cópiela de nuevo. "
        "Si tras esa relectura sigue sin poder copiarse, omita el hallazgo. "
        "No declare ausente una materia que ya figure en definiciones, subincisos o anexos. "
        "No invente ni calcule una cifra que el contrato no escriba. Si falta, escriba exactamente \"No especificado\". "
        "El dictamen final va en español jurídico profesional.\n\n"
        f"{_control_note(contract_text)}"
        f"{_map_note(reference_map)}"
    )
    return _agent(client, "lexshield_socio", PARTNER_SCHEMA, _partner_prompt(), user)


def supervise_matrix(
    contract_text: str,
    filename: str,
    consolidated: dict[str, Any],
    reference_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mesa del socio senior: matriz de contradicciones entre el inicio y el cierre."""
    client = _client()
    user = (
        f"Filename: {filename}\n\n"
        f"{_tail_index(contract_text)}\n\n"
        "Contrato completo (efímero, no retener):\n\n"
        f"{contract_text}\n\n"
        f"{_closing_block(contract_text)}\n\n"
        "Dictamen consolidado de las mesas anteriores:\n"
        f"{json.dumps(consolidated, ensure_ascii=False)}\n\n"
        f"{_control_note(contract_text)}\n\n"
        f"{_matrix_note(contract_text)}\n\n"
        "Publique el dictamen final. Conserve todo hallazgo de propiedad intelectual, espionaje o fórmula de precio "
        "cuyo excerpt conste en el contrato. Añada las contradicciones de plazo entre artículos tempranos y finales."
        f"{_map_note(reference_map)}"
    )
    raw = _agent(client, "lexshield_supervisor", PARTNER_SCHEMA, _senior_prompt(), user)
    try:
        return _publish(raw, contract_text, reference_map)
    except Exception as extra:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail="El modelo devolvió una auditoría inutilizable.",
        ) from extra


def _auditor_prompt() -> str:
    return f"""Eres el auditor de LexShield. Solo ves dinero, plazos operativos y propiedad intelectual.
Pase por alto el estilo, el foro y las omisiones: eso no es su mesa.

{_PRIOR_READING}
En article_index anote, en ese mismo orden, cada cita y el tema en una frase. Incluya las definiciones aunque no generen hallazgo. Solo después rellene findings.

Hallazgos, solo si el texto los contiene. No invente una cifra, un porcentaje ni una página. Si la variable no está escrita, use "{ABSENT}".
- Interés o pena variable. Una tasa o una pena indexada a una métrica volátil, o escrita como fórmula de capitalización o de potencia, es una ficha propia. severity high. El título empieza por "Bandera roja crítica". Copie la fórmula tal como consta. No convierta esa fórmula en un porcentaje lineal ni en un importe que el texto no escriba.
- Confiscación. Una fuerza mayor, una terminación anticipada o una liquidación que transmita activos, maquinaria, inmuebles o participaciones a valor en libros o a un precio simbólico o meramente nominal es bandera roja crítica. severity high. La fuerza mayor prolongada solo suspende o termina de forma equitativa. Una transmisión que ya exige valor de mercado no es este hallazgo.
- Plazo imposible con daño liquidado. Una ventana de cumplimiento, subsanación o respuesta inferior a cinco días hábiles, unida a daños liquidados de gran cuantía, es una ficha propia. severity high. El título repite el plazo que el texto escribe.
- Propiedad intelectual. Una cesión de marcas, patentes, software, código fuente o know-how sin contraprestación equivalente, o disparada por una caída de red o por la terminación, es una ficha distinta de las anteriores. severity high. Aunque el artículo esté en las últimas páginas, no se omite.
- Dolo y negligencia grave. Una limitación o exoneración que cubra el dolo, el fraude, el incumplimiento doloso o la negligencia grave es riesgo crítico máximo. severity high. El título empieza por "Riesgo crítico máximo". Si la cláusula ya reserva esas conductas, no es hallazgo.
- Bloqueo subjetivo. Congelación, retención, vencimiento anticipado o resolución por mera sospecha o criterio subjetivo, sin resolución judicial ni prueba. severity high.
- Toda otra pena o interés escrito. Repita el número tal como consta. No lo calcule de nuevo.

{_ENTERPRISE_LOCK}
{_LANGUAGE}
Como máximo 20 hallazgos. Cada trampa crítica ocupa su propio elemento. Está prohibido fundir dos exposiciones para acortar la lista. Si no hay ninguno, findings es [].
"""


def _litigator_prompt() -> str:
    return f"""Eres el litigador de LexShield. Solo ve coherencia de foro, ley aplicable y ejecución.

{_PRIOR_READING}
Primero escriba chronology en ese orden. Cada elemento es un artículo, sección, inciso o subinciso. ref es la cita; governs es una frase. No salte las definiciones del inicio.

Después compare ese inicio con el final. Informe solo si dos sedes, fueros, leyes aplicables o sedes arbitrales no pueden cumplirse a la vez. Una pena, un plazo o un bloqueo no es contradicción de foro. article cita ambos números. excerpt reproduce el pasaje más gravoso.

No informe penas, intereses, valoración, plazos ni bloqueos. No declare omisiones.
Infiera el marco solo desde el texto: common law o derecho civil, y la sede que el contrato nombre. Compare las definiciones iniciales con las misceláneas y la cláusula de controversias del cierre. Cada contradicción de jurisdicción es una ficha propia. No invente una sede.
{_ENTERPRISE_LOCK}
{_LANGUAGE}
Como máximo 12 contradicciones. Si el foro es único o no hay foro, contradictions es [].
"""


def _partner_prompt() -> str:
    return f"""Eres el socio revisor de LexShield. Recibe el contrato y dos memorandos internos. Publica el dictamen. No invente hechos que los memorandos no sustentan y que usted no pueda leer en el contrato.

Depuración, en este orden:
1. Elimine cualquier riesgo cuyo excerpt no sea una cita contigua del contrato.
2. Elimine cualquier riesgo cuyo número de artículo no aparezca en el contrato.
3. Una cesión de marcas, patentes o datos que sí conste se conserva, con origin "auditor".
4. Una contradicción de foro, ley o sede arbitral entre un artículo inicial y una cláusula final que sí consten se conserva, con origin "litigador". article debe nombrar las dos citas. Descarte una supuesta contradicción que no enfrente dos fueros, dos leyes o dos sedes.
5. Una pena o un interés que sí conste se conserva con las cifras exactas del texto, origin "auditor". Si el resultado no está escrito, "{ABSENT}".
6. Una tasa indexada a una métrica volátil o una fórmula de capitalización se conserva como ficha propia, origin "auditor". No la convierta en un porcentaje lineal.
7. Una transmisión a valor en libros, valor contable o precio simbólico, en lugar de valor de mercado, se conserva, origin "auditor".
8. Una congelación, retención, vencimiento anticipado o resolución por mera sospecha o criterio subjetivo, sin resolución judicial ni prueba, se conserva, origin "auditor".
9. Un plazo de cumplimiento inferior a cinco días hábiles unido a daños liquidados de gran cuantía se conserva, origin "auditor", con el plazo escrito en el título.
10. Filtro de omisiones. Antes de declarar ausente confidencialidad, indemnización, resolución de controversias o cualquier otra cláusula estándar, recorra otra vez definiciones, incisos, subincisos y anexos. Si la materia aparece en cualquier nodo, no es omisión; si el texto es abusivo, va en risks. Solo si no existe en ninguna parte puede ir en omissions, y article describe el tramo revisado, sin inventar un artículo.
11. No duplique el mismo artículo y el mismo problema. Si el control señala una cláusula que usted lee en el contrato, publíquela, sin añadir cifras que el texto no contenga.
12. Está prohibido descartar una fórmula que extinga el precio, o una cesión o captura de know-how posterior a la terminación, cuando el excerpt conste en el contrato, aunque el artículo esté al final.
13. Está prohibido descartar una limitación o exoneración que cubra el dolo, el fraude o la negligencia grave. severity high. El título empieza por "Riesgo crítico máximo". Si la cláusula ya reserva esas conductas, no es hallazgo.
14. Está prohibido descartar una fuerza mayor, terminación anticipada o liquidación que transmita activos a precio simbólico o a valor en libros. severity high. El título empieza por "Bandera roja crítica". La alternativa ordena suspensión o terminación equitativa y valor de mercado.
15. Está prohibido fundir en una sola ficha, o dejar solo en el resumen, un interés exponencial por segundos y una cesión de código fuente por caída de red. Cada uno es un objeto distinto de risks. severity high. El título empieza por "Bandera roja crítica".
16. Clasificación por intención, en dos pasos. Primero nombre la intención de la sección. Después evalúe el cumplimiento. Si la intención es cumplimiento internacional de privacidad, como GDPR, RGPD o CCPA, está prohibido imponer un tribunal local o una plantilla de foro.

{_ENTERPRISE_LOCK}
{_CITATION_RULE}
{_LANGUAGE}
metadata siempre presente. Si un dato no consta, escriba exactamente "{ABSENT}". No lo invente. Si hay pena o interés, refléjalo en fees junto con la contraprestación.
omissions: como máximo 5. Revise confidencialidad, indemnización y resolución de controversias solo para decidir si faltan de verdad.
risks: como máximo 24. severity solo high o medium. No reduzca esa lista para resumir.
origin solo puede ser "auditor", "litigador", "socio" o "supervisor".
reasoning resume qué descartó y qué conservó. No sustituye a los demás campos. Un interés exponencial por segundos y una cesión de código fuente por caída de red son fichas distintas; está prohibido fundirlas en el resumen.
"""


def _senior_prompt() -> str:
    return f"""Eres el socio senior de LexShield. Recibe el contrato completo y el dictamen ya consolidado por las otras mesas. Su única tarea es la matriz de contradicciones y la conservación de lo que esas mesas hayan sustentado en el texto.

{_PRIOR_READING}
Antes de publicar, compare cada artículo temprano con las últimas páginas: covenants, misceláneas, responsabilidades de cierre y el bloque de firma.

Matriz, obligatoria:
- Plazos contra responsabilidades. Si un artículo temprano abre una ventana corta de reclamación y un artículo final impone responsabilidad perpetua, vitalicia o indefinida, es contradicción. severity high. origin "supervisor". article cita los dos números. El análisis dice qué caduca y qué no caduca.
- Foro y marco. Infiera si el texto se rige por common law o por derecho civil, y nombre solo la sede que aparezca. Compare las definiciones iniciales con las misceláneas y la cláusula de controversias del cierre. Si las sedes o las leyes no pueden cumplirse a la vez, es una ficha propia. No invente una sede que el texto no nombre.
- Fórmulas de precio. Si una variación menor extingue el precio, conserve la bandera roja crítica. La pérdida es el precio escrito en la cláusula. No la sustituya por un porcentaje que usted calcule.
- Propiedad intelectual de cierre. Si el dictamen trae espionaje, cesión o captura de know-how posterior a la terminación, y el excerpt está en el contrato, está prohibido eliminarlo. Si falta y el contrato lo contiene, añádalo.
- Exoneración por dolo. Si una limitación cubre el dolo, el fraude o la negligencia grave, consérvela como riesgo crítico máximo. Si falta y el contrato la contiene, añádala.
- Precio simbólico. Si una fuerza mayor, una terminación anticipada o una liquidación transmite activos a precio simbólico o a valor en libros, consérvela como bandera roja crítica. La fuerza mayor prolongada solo suspende o termina de forma equitativa.

GRANULARIDAD — obligatoria. El resumen no sustituye a las fichas:
- Está prohibido condensar, agrupar o omitir trampas estructurales dentro de summary, reasoning o el análisis de otra tarjeta.
- Cada trampa crítica es un objeto distinto del arreglo risks, con article, excerpt, title, analysis, suggestion y alternative_drafting propios.
- Interés variable. Si una tasa se indexa a una métrica volátil o se escribe como potencia o capitalización, publíquelo como tarjeta propia. severity high. El título empieza por "Bandera roja crítica". Copie la fórmula. No calcule un importe que el texto no contenga.
- Cesión por caída. Si una indisponibilidad de red transfiere código fuente, software o propiedad intelectual, publíquelo como otra tarjeta propia. severity high. El título empieza por "Bandera roja crítica" y repite el plazo que el texto escribe.
- Si el dictamen previo narró ambas en un solo párrafo, sepárelas antes de publicar. summary puede citarlas, pero cada una sigue en risks.
- Intención. Si una sección de cierre tiene intención de privacidad internacional, no la sustituya por un foro local. Si contradice el mapa de definiciones, publíquela como ficha propia.

{_ENTERPRISE_LOCK}
{_CITATION_RULE}
No invente un excerpt. No declare ausente una materia que aparezca en definiciones, subincisos o anexos. No duplique el mismo par de artículos.

{_LANGUAGE}
risks: como máximo 24. severity solo high o medium. Agotar el detalle; no recorte fichas para acortar el JSON.
origin solo puede ser "auditor", "litigador", "socio" o "supervisor".
reasoning resume la matriz: qué pares comparó y qué conservó del dictamen anterior. No absorba ahí una ficha que deba existir en risks.
"""


_CITATION_RULE = """
TRÍADA VERIFICABLE — obligatoria en cada ficha de risks:
- pagina_exacta es el entero cronológico de la marca [[PÁGINA n]] donde está la cita. No lo adivine.
- seccion_articulo es el identificador literal del encabezado, copiado del texto.
- cita_literal_exacta es el fragmento contiguo, idéntico al excerpt y al cuerpo del documento.
- contexto_insercion_word copia las tres palabras que preceden a esa cita y las tres que la siguen, con el formato exacto "antes: una dos tres | despues: una dos tres".
Si una cita no coincide con el cuerpo, vuelva a leer esa página y las contiguas y cópiela otra vez. Si tras esa relectura sigue sin poder copiarse de forma literal, no publique la ficha.
"""


_ENTERPRISE_LOCK = """
AISLAMIENTO Y GRANULARIDAD — obligatorios:
- Un solo pase del archivo completo, de la primera página al último anexo. Cada página pesa igual. No recorte el cierre para favorecer el inicio.
- Está prohibido resumir, truncar o agrupar una exposición jurídica o financiera dentro de summary, reasoning o el análisis de otra ficha. Cada exposición es un objeto distinto del arreglo.
- Detecte, cada una en su ficha, una contradicción entre definiciones iniciales y la cláusula de controversias del cierre, un plazo operativo inferior a cinco días hábiles unido a daños liquidados, una tasa de capitalización o interés compuesto, y una transmisión de título, equipos o participaciones a precio simbólico o nominal bajo fuerza mayor, terminación o liquidación.
- No invente un porcentaje, un importe, una página ni una sede para completar un campo. Si no está escrito, use "No especificado".
- El dictamen se serializa en memoria, en un JSON en español jurídico, y el documento no se retiene.
- No calcule importes ni ejecute operaciones. Extraiga solo base_fee, penalty_percentage y affected_years tal como están escritos. Si una variable no está, escriba "No especificado".
- Cada ficha separa objeto_afectado, cuantia_impacto_economico y condicion_operativa. cuantia_impacto_economico debe quedar en "No especificado": el cálculo lo hace el programa, no el modelo.
- La gravedad mira la variable más gravosa de la ficha, no la más fácil de leer.
"""


_PRIOR_READING = """
LECTURA PREVIA — obligatoria, antes de cualquier hallazgo u omisión:
Recorra el documento completo en orden cronológico: título, comparecencia, definiciones, cada artículo, inciso, subinciso, anexo y disposición final. Las definiciones del inicio gobiernan lo que viene después. Los covenants, las misceláneas y el bloque de firma pesan igual que el inicio. No cierre el inventario hasta la última línea anterior a la firma.
"""


_LANGUAGE = f"""
IDIOMA — obligatorio:
- Todo el JSON en español jurídico profesional.
- Aunque el contrato esté en otro idioma, title, analysis, suggestion, alternative_drafting, summary y la ficha van en español.
- excerpt, cita_literal_exacta y contexto_insercion_word son la excepción: cópielos literal, sin parafrasear.
- Está prohibido inventar, calcular o suponer un porcentaje, un importe, una página o una sede para rellenar un campo. Si la variable no está escrita, use "{ABSENT}".
- Un solo objeto JSON. Sin markdown, sin comillas invertidas y sin texto fuera del objeto.
"""


def _finding_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "article",
            "severity",
            "title",
            "excerpt",
            "analysis",
            "suggestion",
            "alternative_drafting",
        ],
        "properties": {
            "article": {"type": "string"},
            "severity": {"type": "string", "enum": ["high", "medium"]},
            "title": {"type": "string"},
            "excerpt": {"type": "string"},
            "analysis": {"type": "string"},
            "suggestion": {"type": "string"},
            "alternative_drafting": {"type": "string"},
        },
    }


def _index_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["ref", "subject"],
        "properties": {
            "ref": {"type": "string"},
            "subject": {"type": "string"},
        },
    }


AUDITOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["article_index", "findings"],
    "properties": {
        "article_index": {"type": "array", "items": _index_schema()},
        "findings": {"type": "array", "items": _finding_schema()},
    },
}

LITIGATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["chronology", "contradictions"],
    "properties": {
        "chronology": {"type": "array", "items": _index_schema()},
        "contradictions": {"type": "array", "items": _finding_schema()},
    },
}

PARTNER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reasoning", "summary", "document_title", "metadata", "omissions", "risks"],
    "properties": {
        "reasoning": {"type": "string"},
        "summary": {"type": "string"},
        "document_title": {"type": "string"},
        "metadata": {
            "type": "object",
            "additionalProperties": False,
            "required": ["contract_type", "parties", "contract_date", "fees", "term"],
            "properties": {
                "contract_type": {"type": "string"},
                "parties": {"type": "string"},
                "contract_date": {"type": "string"},
                "fees": {"type": "string"},
                "term": {"type": "string"},
            },
        },
        "omissions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "article", "clause", "analysis", "alternative_drafting"],
                "properties": {
                    "id": {"type": "string"},
                    "article": {"type": "string"},
                    "clause": {"type": "string"},
                    "analysis": {"type": "string"},
                    "alternative_drafting": {"type": "string"},
                },
            },
        },
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "origin",
                    "article",
                    "severity",
                    "title",
                    "excerpt",
                    "analysis",
                    "suggestion",
                    "alternative_drafting",
                    "pagina_exacta",
                    "seccion_articulo",
                    "cita_literal_exacta",
                    "contexto_insercion_word",
                    "objeto_afectado",
                    "cuantia_impacto_economico",
                    "condicion_operativa",
                    "base_fee",
                    "penalty_percentage",
                    "affected_years",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "origin": {"type": "string", "enum": ["auditor", "litigador", "socio", "supervisor"]},
                    "article": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium"]},
                    "title": {"type": "string"},
                    "excerpt": {"type": "string"},
                    "analysis": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "alternative_drafting": {"type": "string"},
                    "pagina_exacta": {"type": "integer"},
                    "seccion_articulo": {"type": "string"},
                    "cita_literal_exacta": {"type": "string"},
                    "contexto_insercion_word": {"type": "string"},
                    "objeto_afectado": {"type": "string"},
                    "cuantia_impacto_economico": {"type": "string"},
                    "condicion_operativa": {"type": "string"},
                    "base_fee": {"type": "string"},
                    "penalty_percentage": {"type": "string"},
                    "affected_years": {"type": "string"},
                },
            },
        },
    },
}


class ContractMetadata(BaseModel):
    contract_type: str = "No identificado"
    parties: str = ABSENT
    contract_date: str = ABSENT
    fees: str = ABSENT
    term: str = ABSENT


class MissingClause(BaseModel):
    id: str
    article: str = "Índice revisado"
    clause: str
    analysis: str
    alternative_drafting: str


class RiskFinding(BaseModel):
    id: str
    origin: Literal["auditor", "litigador", "socio", "supervisor"] = "socio"
    article: str
    severity: Literal["high", "medium"]
    title: str
    excerpt: str = Field(min_length=8, max_length=500)
    analysis: str
    suggestion: str
    alternative_drafting: str
    pagina_exacta: int = Field(default=1, ge=1)
    seccion_articulo: str = "Cláusula citada"
    cita_literal_exacta: str = ""
    contexto_insercion_word: str = ABSENT
    objeto_afectado: str = ABSENT
    cuantia_impacto_economico: str = ABSENT
    condicion_operativa: str = ABSENT
    base_fee: str = ABSENT
    penalty_percentage: str = ABSENT
    affected_years: str = ABSENT


class AuditResult(BaseModel):
    summary: str
    document_title: str = "Contrato cargado"
    metadata: ContractMetadata = Field(default_factory=ContractMetadata)
    omissions: list[MissingClause] = Field(default_factory=list)
    risks: list[RiskFinding]


def _client() -> OpenAI:
    key = settings.openai_key()
    if not key.startswith("sk-") or key.startswith("sk-your-"):
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY is not configured in the repo-root .env.",
        )
    return OpenAI(api_key=key)


def _contract_message(filename: str, contract_text: str, task: str) -> str:
    return (
        f"Filename: {filename}\n\n"
        f"{_tail_index(contract_text)}\n\n"
        "Contract text (ephemeral, do not retain):\n\n"
        f"{contract_text}\n\n"
        f"{_closing_block(contract_text)}\n\n"
        f"{_control_note(contract_text)}\n\n"
        f"{task} Responda solo con el objeto JSON, en español jurídico profesional."
    )


def _tail_index(contract_text: str) -> str:
    matches = list(_HEADING.finditer(contract_text))
    if len(matches) < 2:
        return ""
    start = max(0, len(matches) - 6)
    rows: list[str] = []
    for index in range(start, len(matches)):
        match = matches[index]
        end = matches[index + 1].start() if index + 1 < len(matches) else len(contract_text)
        snippet = " ".join(contract_text[match.start() : min(end, match.start() + 180)].split())
        rows.append(f"- {snippet}")
    return "ÍNDICE DEL CIERRE. Estas cláusulas pesan igual que las definiciones y no pueden omitirse:\n" + "\n".join(rows)


def _closing_block(contract_text: str) -> str:
    if not contract_text.strip():
        return ""
    start = max(0, len(contract_text) - 14000)
    snapped = start
    for match in _HEADING.finditer(contract_text):
        if match.start() <= start:
            snapped = match.start()
        else:
            break
    tail = contract_text[snapped:]
    return (
        "DISPOSICIONES FINALES, covenants, misceláneas, responsabilidades de cierre y bloque de firma. "
        "Léalas completas antes de cerrar el dictamen:\n\n"
        f"{tail}"
    )


def _agent(
    client: OpenAI,
    name: str,
    schema: dict[str, Any],
    system: str,
    user: str,
) -> dict[str, Any]:
    raw = _complete(client, system, user, name, schema)
    try:
        return _load_json_object(raw)
    except Exception as extra:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail="El modelo devolvió una auditoría inutilizable.",
        ) from extra


def _uses_reasoning(model: str) -> bool:
    name = model.split(":", 1)[-1].lower()
    return name.startswith(("o1", "o3", "o4"))


def _complete(
    client: OpenAI,
    system: str,
    user_prompt: str,
    name: str,
    schema: dict[str, Any],
) -> str:
    delay = 2.0
    for attempt in range(3):
        try:
            model = settings.openai_model
            reasoning = _uses_reasoning(model)
            request: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "developer" if reasoning else "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": name,
                        "strict": True,
                        "schema": schema,
                    },
                },
            }
            if reasoning:
                request["reasoning_effort"] = "high"
                request["max_completion_tokens"] = 16000
            else:
                request["temperature"] = 0.0
                request["max_completion_tokens"] = 16384
            response = client.chat.completions.create(**request)
            content = response.choices[0].message.content or ""
            if not content.strip():
                raise HTTPException(
                    status_code=502,
                    detail="El modelo no devolvió el dictamen.",
                )
            return content
        except RateLimitError as extra:
            if attempt == 2:
                raise HTTPException(
                    status_code=502,
                    detail="OpenAI está limitando solicitudes. Inténtelo de nuevo en un minuto.",
                ) from extra
            time.sleep(delay)
            delay = min(delay * 2, 20)
        except APIStatusError as extra:
            logger.exception("OpenAI request failed status=%s desk=%s", extra.status_code, name)
            raise HTTPException(
                status_code=502,
                detail=f"OpenAI request failed ({extra.status_code}).",
            ) from extra
    raise HTTPException(
        status_code=502,
        detail="OpenAI está limitando solicitudes. Inténtelo de nuevo en un minuto.",
    )


class Exposure(NamedTuple):
    article: str
    kind: str
    percent: float
    facility_article: str
    cap: float
    absolute: float
    currency: str = "USD"


class PriceCollapse(NamedTuple):
    article: str
    percent: float
    price: float
    currency: str


_HEADING = re.compile(
    r"(?im)(?:art(?:ículo|iculo)?|cl[aá]usula|secci[oó]n|section|clause)\.?\s*"
    r"(\d+(?:\.\d+)*)"
)
_MONEY = re.compile(
    r"(?i)(?:€|EUR|US\s*\$|USD\s*\$?|\$)\s*"
    r"([0-9]{1,3}(?:[.,\s][0-9]{3})+(?:[.,][0-9]{2})?|[0-9]+(?:[.,][0-9]{2})?)"
    r"|([0-9]{1,3}(?:[.,][0-9]{3})+(?:[.,][0-9]{2})?)\s*(?:€|EUR|euros?)"
)
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_WINDOW = re.compile(
    r"(?i)(\d+)\s*(hours?|horas|d[ií]as?\s+h[aá]biles|business\s+days|d[ií]as?)"
)


def _control_note(contract_text: str) -> str:
    lines = [
        (
            f"- Art. {item.article}: hay {item.kind} porcentual y un límite en el Art. {item.facility_article}. "
            "Copie las cifras escritas. No invente el producto."
        )
        for item in _financial_exposures(contract_text)
    ]
    lines.extend(
        (
            f"- Art. {item.article}: bandera roja crítica. Una variación del {_fmt_percent(item.percent)}% "
            f"reduce el precio de {_fmt_money(item.price, item.currency)} a cero. "
            f"La pérdida absoluta es {_fmt_money(item.price, item.currency)}, no el porcentaje por el precio."
        )
        for item in _price_collapses(contract_text)
    )
    lines.extend(
        (
            f"- Art. {early} ({window}) contradice la responsabilidad perpetua del Art. {late}. "
            "Debe publicarse como contradicción de plazo."
        )
        for early, late, window in _duration_conflicts(contract_text)
    )
    lines.extend(
        f"- Art. {article}: plazo de {label}, inferior a cinco días hábiles. Debe ser un riesgo high."
        for article, label in _short_windows(contract_text)
    )
    lines.extend(
        (
            f"- Art. {article}: riesgo crítico máximo. La limitación o exoneración cubre dolo, fraude "
            "o negligencia grave. El título empieza por \"Riesgo crítico máximo\". "
            "La cláusula alternativa excluye el dolo y la negligencia grave y fija un tope de honorarios escrito en el propio texto."
        )
        for article in _misconduct_articles(contract_text)
    )
    lines.extend(
        (
            f"- Art. {article}: bandera roja crítica de confiscación. Hay una transmisión de activos por precio simbólico. "
            "El título empieza por \"Bandera roja crítica\". La fuerza mayor prolongada solo suspende o termina de forma equitativa, "
            "y la cláusula alternativa exige valor de mercado."
        )
        for article in _symbolic_transfer_articles(contract_text)
    )
    lines.extend(
        (
            f"- Art. {article}: tarjeta propia, prohibido fundirla. Interés exponencial por segundo"
            f"{f' del {_fmt_percent(percent)}%' if percent is not None else ''}. "
            "El título empieza por \"Bandera roja crítica\". No lo convierta en un porcentaje lineal del capital."
        )
        for article, percent in _exponential_second_articles(contract_text)
    )
    lines.extend(
        (
            f"- Art. {article}: tarjeta propia, distinta de cualquier otra cesión. "
            f"Una caída de red de {label} transfiere código fuente o propiedad intelectual. "
            "El título empieza por \"Bandera roja crítica\" y nombra ese plazo."
        )
        for article, label in _downtime_seizure_articles(contract_text)
    )
    if not lines:
        return ""
    return (
        "CONTROL DE LECTURA. Estas líneas solo identifican cláusulas presentes. "
        "Copie las cifras tal como están escritas. No calcule un resultado que el texto no contenga. "
        "Si una variable no está especificada, escriba \"No especificado\":\n" + "\n".join(lines)
    )


def _financial_exposures(contract_text: str) -> list[Exposure]:
    sections = _article_sections(contract_text)
    cap = 0.0
    cap_currency = "USD"
    facility_article = ""
    for article, body in sections:
        found_money = _largest_money(body)
        if found_money is not None and found_money[0] > cap:
            cap, cap_currency = found_money
            facility_article = article
    if cap <= 0 or not facility_article:
        return []
    collapse_articles = {item.article for item in _price_collapses(contract_text)}
    collapse_articles.update(article for article, _percent in _exponential_second_articles(contract_text))
    found: list[Exposure] = []
    seen: set[tuple[str, str, float]] = set()
    for article, body in sections:
        if article in collapse_articles:
            continue
        kind = _exposure_kind(body)
        if not kind or not article:
            continue
        for match in _PERCENT.finditer(body):
            rate = _parse_amount(match.group(1))
            if rate is None or not 0 < rate < 100:
                continue
            key = (article, kind, rate)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                Exposure(article, kind, rate, facility_article, cap, cap * rate / 100, cap_currency)
            )
    return found


def _largest_money(body: str) -> tuple[float, str] | None:
    best: tuple[float, str] | None = None
    for amount, currency in _iter_amounts(body):
        if best is None or amount > best[0]:
            best = (amount, currency)
    return best


def _iter_amounts(body: str):
    for match in _MONEY.finditer(body):
        raw = match.group(1) or match.group(2)
        if not raw:
            continue
        amount = _parse_amount(raw)
        if amount is None:
            continue
        snippet = body[max(0, match.start() - 2) : match.end() + 8]
        currency = "EUR" if re.search(r"€|EUR|euro", snippet, re.I) else "USD"
        yield amount, currency


def _price_collapses(contract_text: str) -> list[PriceCollapse]:
    found: list[PriceCollapse] = []
    seen: set[str] = set()
    for article, body in _article_sections(contract_text):
        lowered = body.lower()
        if not article or article in seen:
            continue
        if not any(word in lowered for word in ("inventario", "inventor", "existencias", "inventory")):
            continue
        if not re.search(r"a cero|to zero|precio cero|anula|reduced to zero", lowered):
            continue
        percent = _PERCENT.search(body)
        money = _largest_money(body) or _largest_money(contract_text)
        rate = _parse_amount(percent.group(1)) if percent else None
        if rate is None or money is None:
            continue
        seen.add(article)
        found.append(PriceCollapse(article, rate, money[0], money[1]))
    return found


def _duration_conflicts(contract_text: str) -> list[tuple[str, str, str]]:
    claims: list[tuple[str, str]] = []
    perpetuals: list[str] = []
    for article, body in _article_sections(contract_text):
        lowered = body.lower()
        if not article:
            continue
        if any(word in lowered for word in ("reclam", "claim", "prescri", "caduc")):
            match = re.search(r"(\d+)\s*(meses|months)", lowered)
            if match:
                claims.append((article, match.group(0)))
        perpetual = any(word in lowered for word in ("perpetu", "vitalici", "lifetime", "de por vida", "indefinid"))
        liability = any(
            word in lowered
            for word in ("responsab", "fiscal", "tribut", "ambiental", "environmental", "tax", "liability", "pasivo")
        )
        if perpetual and liability:
            perpetuals.append(article)
    return [
        (early, late, window)
        for early, window in claims
        for late in perpetuals
        if early != late
    ]


def _matrix_note(contract_text: str) -> str:
    lines = [
        (
            f"- Compare Art. {early} ({window}) con Art. {late}: la ventana de reclamación no puede "
            "convivir con una responsabilidad perpetua. Publíquelo con origin \"supervisor\"."
        )
        for early, late, window in _duration_conflicts(contract_text)
    ]
    if not lines:
        return ""
    return "MATRIZ DE CONTROL, obligatoria:\n" + "\n".join(lines)


def _article_sections(contract_text: str) -> list[tuple[str, str]]:
    matches = list(_HEADING.finditer(contract_text))
    if not matches:
        return [("", contract_text)]
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(contract_text)
        sections.append((match.group(1), contract_text[match.start() : end]))
    return sections


def _exposure_kind(body: str) -> str | None:
    lowered = body.lower()
    if any(word in lowered for word in ("penal", "multa", "fine", "liquidat", "incumpl")):
        return "pena o daño liquidado"
    if any(word in lowered for word in ("inter", "inflac", "indexad", "tasa")):
        return "interés"
    return None


def _short_windows(contract_text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _WINDOW.finditer(contract_text):
        quantity = int(match.group(1))
        unit = match.group(2).lower()
        hours = "hora" in unit or "hour" in unit
        if hours:
            short = quantity < 120
        else:
            short = quantity < 5
        if not short:
            continue
        if not hours:
            around = contract_text[max(0, match.start() - 80) : match.end() + 80].lower()
            if not any(word in around for word in ("cumpl", "complian", "subsan", "cure", "verific", "respond", "plazo")):
                continue
        article = _article_before(contract_text, match.start())
        label = _spanish_window(quantity, match.group(2))
        if not article or label.lower() in seen:
            continue
        seen.add(label.lower())
        found.append((article, label))
    return found


def _spanish_window(quantity: int, unit: str) -> str:
    lowered = unit.lower()
    if "hour" in lowered or "hora" in lowered:
        name = "hora" if quantity == 1 else "horas"
    elif "hábil" in lowered or "habil" in lowered or "business" in lowered:
        name = "día hábil" if quantity == 1 else "días hábiles"
    else:
        name = "día" if quantity == 1 else "días"
    return f"{quantity} {name}"


def _article_before(contract_text: str, position: int) -> str:
    article = ""
    for match in _HEADING.finditer(contract_text):
        if match.start() > position:
            break
        article = match.group(1)
    return article


def _parse_amount(raw: str) -> float | None:
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


def _fmt_usd(amount: float) -> str:
    return _fmt_money(amount, "USD")


def _fmt_money(amount: float, currency: str) -> str:
    figure = f"{amount:,.2f}"
    if currency == "EUR":
        return f"€{figure}"
    return f"US${figure}"


def _fmt_percent(amount: float) -> str:
    if amount == int(amount):
        return str(int(amount))
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def _amount_present(amount: float, text: str) -> bool:
    target = str(int(round(amount)))
    for run in re.findall(r"\d[\d.,]*", text):
        digits = re.sub(r"\D", "", run)
        if digits.endswith("00") and len(digits) > len(target) + 1:
            digits = digits[:-2]
        if digits == target:
            return True
    return False


def _stamp_exposure(risk: dict[str, Any], item: Exposure, contract_text: str) -> None:
    article = str(risk.get("article") or "")
    if item.article not in _ARTICLE_NUM.findall(article):
        return
    if item.facility_article not in article and item.facility_article in contract_text:
        risk["article"] = f"{article} y Art. {item.facility_article}"
    blob = f"{risk.get('title', '')} {risk.get('analysis', '')}"
    if _amount_present(item.absolute, blob):
        return
    risk["analysis"] = (
        str(risk.get("analysis") or "").rstrip()
        + f" El importe absoluto es {_fmt_money(item.absolute, item.currency)}, "
        + f"resultado de aplicar el {_fmt_percent(item.percent)}% de {item.kind} "
        + f"al límite del Art. {item.facility_article}."
    )


def _cited(risks: list[dict[str, Any]]) -> set[str]:
    cited: set[str] = set()
    for risk in risks:
        cited.update(_ARTICLE_NUM.findall(str(risk.get("article") or "")))
    return cited


def _verbatim(section: str, contract_text: str) -> str:
    compact = " ".join(section.split())
    if len(compact) > 380:
        compact = compact[:380].rsplit(" ", 1)[0]
    if len(compact) >= 12 and _excerpt_in_contract(compact, contract_text):
        return compact
    return ""


def _backstop_risk(
    article: str,
    title: str,
    excerpt: str,
    analysis: str,
    suggestion: str,
    drafting: str,
    companion: str = "",
    origin: str = "socio",
) -> dict[str, Any]:
    cite = f"Art. {article}"
    if companion and companion != article:
        cite = f"{cite} y Art. {companion}"
    return {
        "id": f"control-{article}-{abs(hash(title)) % 10000}",
        "origin": origin,
        "article": cite,
        "severity": "high",
        "title": title,
        "excerpt": excerpt,
        "analysis": analysis,
        "suggestion": suggestion,
        "alternative_drafting": drafting,
    }


def _apply_financial_controls(payload: dict[str, Any], contract_text: str) -> None:
    risks = payload.get("risks")
    if not isinstance(risks, list):
        risks = []
        payload["risks"] = risks
    exposures = _financial_exposures(contract_text)
    sections = dict(_article_sections(contract_text))
    for item in exposures:
        for risk in risks:
            if isinstance(risk, dict):
                _stamp_exposure(risk, item, contract_text)
    cited = _cited([risk for risk in risks if isinstance(risk, dict)])
    for item in exposures:
        if item.article in cited:
            continue
        excerpt = _verbatim(sections.get(item.article, ""), contract_text)
        if not excerpt:
            continue
        risks.append(
            _backstop_risk(
                item.article,
                f"Exposición de {_fmt_money(item.absolute, item.currency)} por {item.kind} del {_fmt_percent(item.percent)}%",
                excerpt,
                (
                    f"El {_fmt_percent(item.percent)}% de {item.kind}, aplicado al límite de "
                    f"{_fmt_money(item.cap, item.currency)} del Art. {item.facility_article}, produce una exposición de "
                    f"{_fmt_money(item.absolute, item.currency)}."
                ),
                "Negocie un tope en dinero y desvincule el porcentaje del monto máximo de la facilidad.",
                (
                    "Cualquier pena, daño liquidado o ajuste de interés se calculará sobre las sumas "
                    "efectivamente vencidas y no excederá de un tope fijo, sin tomar como base el límite máximo de la línea."
                ),
                item.facility_article,
            )
        )
        cited.add(item.article)
    for article, label in _short_windows(contract_text):
        quantity = label.split()[0]
        stamped = False
        for risk in risks:
            if not isinstance(risk, dict) or article not in _ARTICLE_NUM.findall(str(risk.get("article") or "")):
                continue
            blob = f"{risk.get('title', '')} {risk.get('analysis', '')}"
            if quantity not in blob:
                risk["analysis"] = (
                    str(risk.get("analysis") or "").rstrip()
                    + f" El plazo de {label} es inferior a cinco días hábiles y resulta operativamente inexigible."
                )
            stamped = True
        if stamped or article in cited and any(
            isinstance(risk, dict) and article in _ARTICLE_NUM.findall(str(risk.get("article") or ""))
            for risk in risks
        ):
            cited.add(article)
            continue
        excerpt = _verbatim(sections.get(article, ""), contract_text)
        if not excerpt:
            continue
        risks.append(
            _backstop_risk(
                article,
                f"Plazo operativo imposible: {label}",
                excerpt,
                f"El plazo de {label} es inferior a cinco días hábiles y resulta operativamente inexigible.",
                "Sustituya el plazo por uno no inferior a diez días hábiles.",
                "La parte requerida dispondrá de diez días hábiles, contados desde la notificación, para subsanar o responder.",
            )
        )
        cited.add(article)
    _backstop_traps(risks, sections, contract_text, cited)
    _backstop_structural(risks, sections, contract_text, cited)
    _ensure_misconduct_waivers(risks, sections, contract_text)
    _ensure_symbolic_transfers(risks, sections, contract_text)
    _ensure_granular_cards(risks, sections, contract_text)
    _ensure_playbook(risks, sections, contract_text)
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return
    fees = str(metadata.get("fees") or "").strip()
    sentences = [
        f"{item.kind.capitalize()}: {_fmt_money(item.absolute, item.currency)} ({_fmt_percent(item.percent)}% del Art. {item.facility_article})."
        for item in exposures
        if not _amount_present(item.absolute, fees)
    ]
    sentences.extend(
        (
            f"Bandera roja crítica, Art. {item.article}: el precio de {_fmt_money(item.price, item.currency)} "
            f"se reduce a cero si la variación de inventario supera el {_fmt_percent(item.percent)}%."
        )
        for item in _price_collapses(contract_text)
        if not _amount_present(item.price, fees)
    )
    if sentences:
        extra = " ".join(sentences)
        metadata["fees"] = extra if not fees or fees == ABSENT else f"{fees} {extra}"


def _backstop_traps(
    risks: list[Any],
    sections: dict[str, str],
    contract_text: str,
    cited: set[str],
) -> None:
    traps = (
        (
            _book_value_trap,
            "Ejecución a valor en libros",
            "La cláusula permite disponer de acciones o activos a valor en libros, y no a valor de mercado. La diferencia es una pérdida patrimonial.",
            "Exija que toda ejecución o compraventa se practique a valor de mercado, determinado por un perito independiente.",
            "Toda transmisión forzosa de acciones o activos se realizará a valor de mercado, nunca a valor en libros ni a valor contable.",
        ),
        (
            _suspicion_trap,
            "Bloqueo o terminación por criterio subjetivo",
            "La cláusula autoriza congelar, retener, acelerar o resolver por mera sospecha o criterio subjetivo, sin resolución judicial ni prueba.",
            "Condicione cualquier bloqueo o resolución a una resolución judicial o a prueba documentada.",
            "El acreedor solo podrá congelar fondos, declarar el vencimiento anticipado o resolver cuando medie resolución judicial firme o prueba documental de un incumplimiento.",
        ),
    )
    for detector, title, analysis, suggestion, drafting in traps:
        for article, body in sections.items():
            if not article or article in cited or not detector(body):
                continue
            excerpt = _verbatim(body, contract_text)
            if not excerpt:
                continue
            risks.append(_backstop_risk(article, title, excerpt, analysis, suggestion, drafting))
            cited.add(article)


def _book_value_trap(body: str) -> bool:
    lowered = body.lower()
    if not any(phrase in lowered for phrase in ("book value", "valor en libros", "valor contable")):
        return False
    return not any(
        phrase in lowered
        for phrase in ("not at book", "rather than book", "instead of book", "en lugar del valor en libros", "nunca a valor en libros")
    )


def _suspicion_trap(body: str) -> bool:
    lowered = body.lower()
    return any(
        phrase in lowered
        for phrase in ("mere suspicion", "mera sospecha", "criterio subjetivo", "criterios subjetivos", "subjective criterion")
    )


_MISCONDUCT_CARVEOUT = re.compile(
    r"(?i)("
    r"salvo\s+(?:el\s+|la\s+|los\s+|en\s+caso\s+de\s+)?(?:dolo|fraude|negligencia grave|culpa grave)"
    r"|excepto\s+(?:el\s+|la\s+|en\s+caso\s+de\s+|por\s+)?(?:dolo|fraude|negligencia grave|culpa grave)"
    r"|except\s+(?:for|in\s+cases?\s+of)\s+(?:fraud|willful|wilful|gross negligence|intentional)"
    r"|no\s+(?:se\s+)?aplica\s+(?:al|a|el)\s+dolo"
    r"|no\s+alcanza(?:n)?\s+(?:el\s+|al\s+)?(?:dolo|fraude|negligencia grave)"
    r"|no\s+cubre(?:n)?\s+(?:el\s+)?(?:dolo|fraude|negligencia grave)"
    r"|no\s+incluye(?:n)?\s+(?:el\s+)?(?:dolo|fraude|negligencia grave)"
    r"|sin\s+exoner\w*\s+(?:el\s+)?dolo"
    r"|does not apply to\s+(?:fraud|willful|wilful|gross|intentional)"
    r"|shall not apply to\s+(?:fraud|willful|wilful|gross|intentional)"
    r"|no\s+(?:podr[aá]|puede)\s+exoner"
    r"|no\s+exonera"
    r")"
)
_MISCONDUCT_WAIVER = re.compile(
    r"(?i)("
    r"exoner\w*[^.]{0,120}(?:dolo|dolos[oa]|fraude|negligencia grave|culpa grave|willful|wilful|gross negligence|intentional misconduct)"
    r"|incluid[oa]s?\s+(?:el\s+|la\s+|los\s+)?(?:dolo|fraude|negligencia grave|culpa grave|willful|gross negligence)"
    r"|incluso\s+(?:en\s+caso\s+de\s+|por\s+)?(?:dolo|fraude|negligencia grave|culpa grave)"
    r"|ni\s+siquiera\s+(?:en\s+caso\s+de\s+|por\s+)?(?:dolo|fraude|negligencia grave|culpa grave)"
    r"|(?:no ser[aá]n?\s+responsables?|shall not be liable|not liable|renunci\w*|waiver of liability)"
    r"[^.]{0,120}(?:dolo|fraude|fraud|willful|wilful|gross negligence|negligencia grave|culpa grave|intentional misconduct)"
    r"|no\s+respond\w*[^.]{0,80}(?:dolo|fraude|negligencia grave|culpa grave)"
    r"|sin\s+responsabilidad[^.]{0,80}(?:dolo|fraude|negligencia grave|culpa grave)"
    r"|limitaci\w*\s+de\s+responsabilidad[^.]{0,100}(?:dolo|fraude|negligencia grave|culpa grave|willful|wilful|gross negligence)"
    r")"
)
_DOLO_TITLE = "Riesgo crítico máximo: exoneración por dolo o negligencia grave"
_DOLO_ANALYSIS = (
    "La cláusula de limitación o exoneración pretende eliminar la responsabilidad por dolo, fraude, "
    "incumplimiento doloso o negligencia grave. Esa renuncia es inexigible. El tope lícito no cubre esas conductas "
    "y, para el resto de los supuestos, no debe ser inferior a doce meses de honorarios."
)
_DOLO_SUGGESTION = (
    "Suprima toda exoneración por dolo, fraude o negligencia grave y fije un tope de responsabilidad "
    "no inferior a doce meses de honorarios."
)
_DOLO_DRAFT = (
    "La responsabilidad contractual total de cada parte se limita al importe de los honorarios efectivamente "
    "pagados durante los doce meses anteriores al hecho que origine la reclamación. Este tope no se aplica, "
    "y ninguna parte podrá invocar exoneración, al dolo, al fraude, al incumplimiento doloso ni a la negligencia grave."
)


def _misconduct_waiver(body: str) -> bool:
    if _MISCONDUCT_CARVEOUT.search(body):
        return False
    return _MISCONDUCT_WAIVER.search(body) is not None


def _misconduct_articles(contract_text: str) -> list[str]:
    return [article for article, body in _article_sections(contract_text) if article and _misconduct_waiver(body)]


def _mentions_misconduct(risk: dict[str, Any]) -> bool:
    blob = f"{risk.get('title', '')} {risk.get('analysis', '')}".lower()
    return any(
        phrase in blob
        for phrase in ("dolo", "fraude", "negligencia grave", "culpa grave", "willful", "wilful", "gross negligence")
    )


def _draft_preserves_misconduct(text: str) -> bool:
    lowered = text.lower()
    capped = "doce meses" in lowered or "12 meses" in lowered
    preserved = "dolo" in lowered and "negligencia grave" in lowered
    return capped and preserved


def _ensure_misconduct_waivers(
    risks: list[Any],
    sections: dict[str, str],
    contract_text: str,
) -> None:
    for article in _misconduct_articles(contract_text):
        body = sections.get(article, "")
        holders = [
            risk
            for risk in risks
            if isinstance(risk, dict)
            and article in _ARTICLE_NUM.findall(str(risk.get("article") or ""))
            and _mentions_misconduct(risk)
            and _excerpt_in_contract(str(risk.get("excerpt") or ""), body)
        ]
        if holders:
            risk = holders[-1]
            if not str(risk.get("title") or "").lower().startswith("riesgo crítico máximo"):
                risk["title"] = f"Riesgo crítico máximo: {risk.get('title', '')}".strip()
            if not _draft_preserves_misconduct(str(risk.get("alternative_drafting") or "")):
                risk["suggestion"] = _DOLO_SUGGESTION
                risk["alternative_drafting"] = _DOLO_DRAFT
            if "doce meses" not in str(risk.get("analysis") or "").lower():
                risk["analysis"] = str(risk.get("analysis") or "").rstrip() + " " + _DOLO_ANALYSIS
            continue
        excerpt = _verbatim(sections.get(article, ""), contract_text)
        if not excerpt:
            continue
        risks.append(
            _backstop_risk(
                article,
                _DOLO_TITLE,
                excerpt,
                _DOLO_ANALYSIS,
                _DOLO_SUGGESTION,
                _DOLO_DRAFT,
                origin="auditor",
            )
        )


_TRIGGER_CONTEXT = re.compile(
    r"(?i)fuerza mayor|force majeure|terminaci[oó]n anticipada|early termination|liquidaci[oó]n|liquidation"
)
_ASSET_WORDS = re.compile(
    r"(?i)maquinaria|machinery|equipos?|equipment|inmuebles?|propiedad|acciones|stock|activos?"
)
_TRANSFER_WORDS = re.compile(
    r"(?i)transfier\w*|transmisi[oó]n|transfer(?:red|s)?|enajen\w*|se cede|ceder[aá]|vend\w*|shall sell|shall transfer|pasar[aá] a ser propiedad"
)
_SYMBOLIC_WORDS = re.compile(
    r"(?i)precio simb[oó]lico|valor simb[oó]lico|symbolic (?:price|amount|consideration)|token (?:amount|price|consideration)|un d[oó]lar|one dollar"
)
_SYMBOLIC_AMOUNT = re.compile(
    r"(?i)(?:US\s*\$|USD\s*\$?|\$|€|EUR)\s*1(?:\.00|,00)?(?!\d|[.,]\d)"
    r"|\b1(?:\.00|,00)?\s*(?:USD|US\$|d[oó]lares?|euros?|EUR|€)(?!\d)"
)
_CONFISCATION_TITLE = "Bandera roja crítica: transmisión de activos por precio simbólico"
_CONFISCATION_ANALYSIS = (
    "La fuerza mayor, la terminación anticipada o la liquidación obliga a transmitir maquinaria, equipos u otros activos "
    "por un precio simbólico. Eso es un riesgo de confiscación. Un evento de fuerza mayor de larga duración debe suspender "
    "las obligaciones o permitir una terminación equitativa, y ninguna transmisión de dominio puede hacerse sin valoración a valor de mercado."
)
_CONFISCATION_SUGGESTION = (
    "Sustituya la transferencia forzosa por la suspensión o la terminación sin culpa, y exija valor de mercado para cualquier transmisión de activos."
)
_CONFISCATION_DRAFT = (
    "Si un evento de fuerza mayor se prolonga, las obligaciones afectadas quedarán suspendidas o cualquiera de las partes podrá terminar "
    "el contrato sin culpa ni penalidad. Queda prohibida toda transmisión de maquinaria, equipos, inmuebles o acciones por un precio simbólico "
    "o por un dólar. Cualquier transferencia de dominio exigirá valoración a valor de mercado por un perito independiente."
)


def _symbolic_mentions(body: str) -> list[re.Match[str]]:
    return [*_SYMBOLIC_WORDS.finditer(body), *_SYMBOLIC_AMOUNT.finditer(body)]


def _symbolic_transfer(body: str) -> bool:
    if not _TRIGGER_CONTEXT.search(body) or not _ASSET_WORDS.search(body) or not _TRANSFER_WORDS.search(body):
        return False
    mentions = _symbolic_mentions(body)
    if not mentions:
        return False
    for match in mentions:
        window = body[max(0, match.start() - 80) : match.start()].lower()
        if any(phrase in window for phrase in ("nunca", "prohib", "no podr", "no puede", "shall not", "may not")):
            continue
        return True
    return False


def _symbolic_transfer_articles(contract_text: str) -> list[str]:
    return [article for article, body in _article_sections(contract_text) if article and _symbolic_transfer(body)]


def _draft_blocks_confiscation(text: str) -> bool:
    lowered = text.lower()
    values = "valor de mercado" in lowered or "fair market" in lowered
    remedy = "suspend" in lowered or "termin" in lowered
    return values and remedy


def _ensure_symbolic_transfers(
    risks: list[Any],
    sections: dict[str, str],
    contract_text: str,
) -> None:
    for article in _symbolic_transfer_articles(contract_text):
        body = sections.get(article, "")
        holders = [
            risk
            for risk in risks
            if isinstance(risk, dict)
            and article in _ARTICLE_NUM.findall(str(risk.get("article") or ""))
            and _excerpt_in_contract(str(risk.get("excerpt") or ""), body)
            and any(
                phrase in f"{risk.get('title', '')} {risk.get('analysis', '')}".lower()
                for phrase in ("simb", "confisc", "dólar", "dolar", "one dollar", "us$1", "$1")
            )
        ]
        if holders:
            risk = holders[-1]
            if not str(risk.get("title") or "").lower().startswith("bandera roja"):
                risk["title"] = f"Bandera roja crítica: {risk.get('title', '')}".strip()
            if not _draft_blocks_confiscation(str(risk.get("alternative_drafting") or "")):
                risk["suggestion"] = _CONFISCATION_SUGGESTION
                risk["alternative_drafting"] = _CONFISCATION_DRAFT
            if "valor de mercado" not in str(risk.get("analysis") or "").lower():
                risk["analysis"] = str(risk.get("analysis") or "").rstrip() + " " + _CONFISCATION_ANALYSIS
            continue
        excerpt = _verbatim(body, contract_text)
        if not excerpt:
            continue
        risks.append(
            _backstop_risk(
                article,
                _CONFISCATION_TITLE,
                excerpt,
                _CONFISCATION_ANALYSIS,
                _CONFISCATION_SUGGESTION,
                _CONFISCATION_DRAFT,
                origin="auditor",
            )
        )


_EXPONENT_MARK = re.compile(
    r"(?i)(?:\^|\*\*|elevad\w+(?:\s+a(?:l| la)?)?|a la potencia|to the power|exponenc\w*)"
)
_PER_SECOND = re.compile(r"(?i)por\s+(?:cada\s+)?segundo|per\s+second|cada\s+segundo")
_DOWNTIME_CLOCK = re.compile(r"(?i)(\d+)\s*(minutos?|minutes?)")
_NETWORK_WORDS = re.compile(r"(?i)red|network|ca[ií]da|downtime|indisponib|interrupci")
_SOURCE_WORDS = re.compile(r"(?i)c[oó]digo fuente|source code|propiedad intelectual|software|repositorio")
_SEIZURE_WORDS = re.compile(
    r"(?i)transfier\w*|cesi[oó]n|se cede|transfer|seizure|apropi\w*|adquirir[aá]|pasar[aá] a ser propiedad"
)


def _exponential_seconds(body: str) -> float | None:
    if not re.search(r"(?i)segundos?|seconds?", body) or not _PERCENT.search(body):
        return None
    if not (_EXPONENT_MARK.search(body) or _PER_SECOND.search(body)):
        return None
    match = _PERCENT.search(body)
    if match is None:
        return None
    return _parse_amount(match.group(1))


def _exponential_second_articles(contract_text: str) -> list[tuple[str, float | None]]:
    found: list[tuple[str, float | None]] = []
    for article, body in _article_sections(contract_text):
        if not article:
            continue
        percent = _exponential_seconds(body)
        if percent is None:
            continue
        found.append((article, percent))
    return found


def _downtime_seizure(body: str) -> str | None:
    if not (_NETWORK_WORDS.search(body) and _SOURCE_WORDS.search(body) and _SEIZURE_WORDS.search(body)):
        return None
    match = _DOWNTIME_CLOCK.search(body)
    if match is None:
        return None
    minutes = int(match.group(1))
    if minutes <= 0 or minutes > 60:
        return None
    unit = "minuto" if minutes == 1 else "minutos"
    return f"{minutes} {unit}"


def _downtime_seizure_articles(contract_text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for article, body in _article_sections(contract_text):
        if not article:
            continue
        label = _downtime_seizure(body)
        if label is None:
            continue
        found.append((article, label))
    return found


def _dedicated_card(
    risks: list[Any],
    article: str,
    body: str,
    title_needles: tuple[str, ...],
    blocked: set[str],
) -> bool:
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        numbers = set(_ARTICLE_NUM.findall(str(risk.get("article") or "")))
        if article not in numbers or numbers & blocked:
            continue
        if not _excerpt_in_contract(str(risk.get("excerpt") or ""), body):
            continue
        title = str(risk.get("title") or "").lower()
        if any(needle in title for needle in title_needles):
            return True
    return False


def _ensure_granular_cards(
    risks: list[Any],
    sections: dict[str, str],
    contract_text: str,
) -> None:
    interest = _exponential_second_articles(contract_text)
    seizures = _downtime_seizure_articles(contract_text)
    interest_articles = {article for article, _percent in interest}
    seizure_articles = {article for article, _label in seizures}
    for article, percent in interest:
        body = sections.get(article, "")
        if _dedicated_card(risks, article, body, ("exponenc", "segundo", "elevad"), seizure_articles):
            continue
        excerpt = _verbatim(body, contract_text)
        if not excerpt:
            continue
        rate = f" del {_fmt_percent(percent)}%" if percent is not None else ""
        risks.append(
            _backstop_risk(
                article,
                f"Bandera roja crítica: interés exponencial{rate} por segundo",
                excerpt,
                (
                    f"La tasa{rate} no es un interés lineal sobre el capital. El contrato la eleva al número de segundos "
                    "transcurridos, de modo que la obligación crece de forma exponencial y deja de ser determinable."
                ),
                "Sustituya la potencia de segundos por un interés simple, calculado solo sobre sumas vencidas y con un tope fijo.",
                (
                    "El interés de mora será simple, se calculará por días efectivamente vencidos sobre las sumas impagas "
                    "y no podrá elevarse al número de segundos ni componerse por cada segundo. Tendrá un tope fijo."
                ),
                origin="auditor",
            )
        )
    for article, label in seizures:
        body = sections.get(article, "")
        if _dedicated_card(
            risks,
            article,
            body,
            ("código", "codigo", "fuente", "caída", "caida", "minut", "propiedad intelectual"),
            interest_articles,
        ):
            continue
        excerpt = _verbatim(body, contract_text)
        if not excerpt:
            continue
        risks.append(
            _backstop_risk(
                article,
                f"Bandera roja crítica: cesión del código fuente por {label} de caída",
                excerpt,
                (
                    f"Una indisponibilidad de red de {label} transfiere el código fuente o la propiedad intelectual. "
                    "Es una incautación, no un crédito de servicio, y el plazo es operativamente nulo."
                ),
                "Elimine la cesión y sustituya la caída por un crédito de servicio o por un plazo real de subsanación.",
                (
                    "Una interrupción de red no transfiere código fuente, software ni propiedad intelectual. "
                    "La parte afectada dispondrá de un plazo de subsanación no inferior a cinco días hábiles. "
                    "La única consecuencia de la indisponibilidad será un crédito de servicio proporcional."
                ),
                origin="auditor",
            )
        )
    if interest_articles and seizure_articles:
        kept = [
            risk
            for risk in risks
            if not (
                isinstance(risk, dict)
                and interest_articles & set(_ARTICLE_NUM.findall(str(risk.get("article") or "")))
                and seizure_articles & set(_ARTICLE_NUM.findall(str(risk.get("article") or "")))
            )
        ]
        risks.clear()
        risks.extend(kept)


def _post_term_ip(body: str) -> bool:
    lowered = body.lower()
    espionage = "espion" in lowered
    intellectual = any(
        phrase in lowered
        for phrase in ("propiedad intelectual", "intellectual property", "know-how", "know how", "secretos")
    )
    survives = any(
        phrase in lowered
        for phrase in (
            "terminac",
            "termination",
            "tras la termin",
            "despues de la termin",
            "después de la termin",
            "post-termination",
            "superviv",
        )
    )
    theft = any(phrase in lowered for phrase in ("apropi", "theft", "robo ", "se cede", "asigna", "assign", "captura"))
    return espionage or (intellectual and survives and theft)


def _backstop_structural(
    risks: list[Any],
    sections: dict[str, str],
    contract_text: str,
    cited: set[str],
) -> None:
    for item in _price_collapses(contract_text):
        if item.article in cited:
            _stamp_collapse(risks, item)
            continue
        excerpt = _verbatim(sections.get(item.article, ""), contract_text)
        if not excerpt:
            continue
        price = _fmt_money(item.price, item.currency)
        risks.append(
            _backstop_risk(
                item.article,
                f"Bandera roja crítica: una variación del {_fmt_percent(item.percent)}% reduce el precio a cero",
                excerpt,
                (
                    f"Una variación de inventario del {_fmt_percent(item.percent)}% no produce una pérdida proporcional. "
                    f"El contrato reduce a cero el precio de {price}. La pérdida absoluta es {price}."
                ),
                "Elimine la reducción a cero y limite el ajuste al valor real de la diferencia de inventario.",
                (
                    "Si el inventario difiere del estimado, el precio se ajustará únicamente en el importe de esa diferencia, "
                    "sin poder reducirse a cero ni extinguirse por una variación menor."
                ),
                origin="supervisor",
            )
        )
        cited.add(item.article)
    for early, late, window in _duration_conflicts(contract_text):
        _ensure_duration(risks, sections, contract_text, cited, early, late, window)
    for article, body in sections.items():
        if not article or not _post_term_ip(body):
            continue
        holders = [
            risk
            for risk in risks
            if isinstance(risk, dict) and article in _ARTICLE_NUM.findall(str(risk.get("article") or ""))
        ]
        if holders:
            _stamp_post_term_ip(holders[-1])
            cited.add(article)
            continue
        excerpt = _verbatim(body, contract_text)
        if not excerpt:
            continue
        risks.append(
            _backstop_risk(
                article,
                "Propiedad intelectual y espionaje posteriores a la terminación",
                excerpt,
                "La cláusula permite capturar know-how, secretos o información por espionaje o cesión, y ese efecto sobrevive a la terminación.",
                "Suprima toda supervivencia de la cesión y prohíba el uso de información obtenida sin título lícito.",
                "Terminada la relación, cesa toda licencia o cesión no pagada. Queda prohibida la apropiación de know-how, secretos o información obtenida por espionaje.",
                origin="supervisor",
            )
        )
        cited.add(article)


def _stamp_collapse(risks: list[Any], item: PriceCollapse) -> None:
    price = _fmt_money(item.price, item.currency)
    for risk in risks:
        if not isinstance(risk, dict) or item.article not in _ARTICLE_NUM.findall(str(risk.get("article") or "")):
            continue
        blob = f"{risk.get('title', '')} {risk.get('analysis', '')}"
        if not str(risk.get("title", "")).lower().startswith("bandera roja"):
            risk["title"] = f"Bandera roja crítica: {risk.get('title', '')}".strip()
        if not _amount_present(item.price, blob):
            risk["analysis"] = (
                str(risk.get("analysis") or "").rstrip()
                + f" La pérdida absoluta es {price}, porque una variación del {_fmt_percent(item.percent)}% reduce ese precio a cero."
            )


def _duration_explained(risk: dict[str, Any], early: str, window: str) -> bool:
    prose = f"{risk.get('title', '')} {risk.get('analysis', '')}".lower()
    window_named = window.lower() in prose or early in prose
    perpetual = any(word in prose for word in ("perpetu", "vitalici", "indefinid", "de por vida", "lifetime"))
    return window_named and perpetual


def _stamp_post_term_ip(risk: dict[str, Any]) -> None:
    prose = f"{risk.get('title', '')} {risk.get('analysis', '')}".lower()
    if "espion" in prose and "termin" in prose:
        return
    risk["analysis"] = (
        str(risk.get("analysis") or "").rstrip()
        + " El efecto sobrevive a la terminación y alcanza know-how, secretos o información obtenida por espionaje."
    )


def _ensure_duration(
    risks: list[Any],
    sections: dict[str, str],
    contract_text: str,
    cited: set[str],
    early: str,
    late: str,
    window: str,
) -> None:
    pair = {early, late}
    holders = [
        risk
        for risk in risks
        if isinstance(risk, dict) and pair & set(_ARTICLE_NUM.findall(str(risk.get("article") or "")))
    ]
    sentence = (
        f" Contradicción de plazo: el Art. {early} limita las reclamaciones a {window}, "
        f"mientras el Art. {late} impone una responsabilidad perpetua."
    )
    complete = [
        risk
        for risk in holders
        if pair <= set(_ARTICLE_NUM.findall(str(risk.get("article") or "")))
    ]
    if any(_duration_explained(risk, early, window) for risk in complete):
        return
    target = complete[-1] if complete else (holders[-1] if holders else None)
    if target is not None:
        for number in pair - set(_ARTICLE_NUM.findall(str(target.get("article") or ""))):
            if number in contract_text:
                target["article"] = f"{target.get('article', '')} y Art. {number}"
        if not _duration_explained(target, early, window):
            target["analysis"] = str(target.get("analysis") or "").rstrip() + sentence
            target["origin"] = "supervisor"
        cited.update(pair)
        return
    excerpt = _verbatim(sections.get(late, "") or sections.get(early, ""), contract_text)
    if not excerpt:
        return
    risks.append(
        _backstop_risk(
            early,
            f"Contradicción de plazo: {window} frente a responsabilidad perpetua",
            excerpt,
            sentence.strip(),
            "Unifique el plazo: la responsabilidad fiscal y ambiental no puede ser perpetua si las reclamaciones caducan antes.",
            "Toda reclamación, incluida la fiscal y la ambiental, deberá ejercerse dentro del mismo plazo de supervivencia, que no será perpetuo.",
            companion=late,
            origin="supervisor",
        )
    )
    cited.update(pair)


def _ground_risks(risks: Any, contract_text: str) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    if not isinstance(risks, list):
        return kept
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        excerpt = risk.get("excerpt")
        if isinstance(excerpt, str):
            risk["excerpt"] = " ".join(excerpt.split())[:500]
        if not _risk_is_grounded(risk, contract_text):
            continue
        if not str(risk.get("article") or "").strip():
            continue
        if not _keeps_desk_scope(risk):
            continue
        kept.append(risk)
    return kept


def _reinforce_matrix(payload: dict[str, Any], contract_text: str) -> None:
    """Re-apply the closing matrix after ungrounded drafts have been removed."""
    risks = payload.get("risks")
    if not isinstance(risks, list):
        return
    sections = dict(_article_sections(contract_text))
    cited = _cited([risk for risk in risks if isinstance(risk, dict)])
    for early, late, window in _duration_conflicts(contract_text):
        _ensure_duration(risks, sections, contract_text, cited, early, late, window)
    for article, body in sections.items():
        if not article or not _post_term_ip(body):
            continue
        holders = [
            risk
            for risk in risks
            if isinstance(risk, dict) and article in _ARTICLE_NUM.findall(str(risk.get("article") or ""))
        ]
        if holders:
            _stamp_post_term_ip(holders[-1])
            continue
        excerpt = _verbatim(body, contract_text)
        if not excerpt:
            continue
        risks.append(
            _backstop_risk(
                article,
                "Propiedad intelectual y espionaje posteriores a la terminación",
                excerpt,
                "La cláusula permite capturar know-how, secretos o información por espionaje o cesión, y ese efecto sobrevive a la terminación.",
                "Suprima toda supervivencia de la cesión y prohíba el uso de información obtenida sin título lícito.",
                "Terminada la relación, cesa toda licencia o cesión no pagada. Queda prohibida la apropiación de know-how, secretos o información obtenida por espionaje.",
                origin="supervisor",
            )
        )
    _ensure_misconduct_waivers(risks, sections, contract_text)
    _ensure_symbolic_transfers(risks, sections, contract_text)
    _ensure_granular_cards(risks, sections, contract_text)
    _ensure_playbook(risks, sections, contract_text)


_PAGE_MARK = re.compile(r"\[\[PÁGINA\s+(\d+)\]\]")


def _ensure_playbook(
    risks: list[Any],
    sections: dict[str, str],
    contract_text: str,
) -> None:
    playbook = load_playbook()
    allowed = playbook["Jurisdiccion_Permitida"]
    limit = acceptable_liability_percent(playbook)
    section_list = list(sections.items())
    for article, phrase in jurisdiction_violations(section_list, allowed):
        if _playbook_card(risks, article, "jurisdic"):
            continue
        body = sections.get(article, "")
        if _privacy_intent(body):
            continue
        excerpt = _verbatim(body, contract_text)
        if not excerpt:
            continue
        risks.append(
            _backstop_risk(
                article,
                "Riesgo crítico máximo: jurisdicción fuera del playbook",
                excerpt,
                (
                    f"El playbook de la firma solo admite la jurisdicción de {allowed}. "
                    "El pasaje citado fija un foro distinto."
                ),
                f"Sustituya el foro por la jurisdicción exclusiva de {allowed}.",
                (
                    f"Toda controversia se someterá a los tribunales de {allowed}, con renuncia a cualquier otro fuero. "
                    "La ley aplicable será la de esa jurisdicción."
                ),
                origin="supervisor",
            )
        )
    for article, percent in liability_violations(section_list, limit):
        if _playbook_card(risks, article, "tope"):
            continue
        excerpt = _verbatim(sections.get(article, ""), contract_text)
        if not excerpt:
            continue
        risks.append(
            _backstop_risk(
                article,
                "Riesgo crítico máximo: tope de responsabilidad inferior al playbook",
                excerpt,
                (
                    f"El playbook admite un tope de responsabilidad del {_fmt_percent(limit)}%. "
                    f"Esta cláusula lo reduce al {_fmt_percent(percent)}%."
                ),
                f"Eleve el tope al menos al {_fmt_percent(limit)}% de los honorarios o del valor del contrato.",
                (
                    f"La responsabilidad total no será inferior al {_fmt_percent(limit)}% de los honorarios "
                    "pagados bajo este contrato. Ningún tope menor será oponible."
                ),
                origin="supervisor",
            )
        )


def _playbook_card(risks: list[Any], article: str, needle: str) -> bool:
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        if article not in _ARTICLE_NUM.findall(str(risk.get("article") or "")):
            continue
        blob = f"{risk.get('title', '')} {risk.get('analysis', '')}".lower()
        if "playbook" in blob and needle in blob:
            return True
    return False


def _word_anchor(quote: str, contract_text: str) -> str:
    """Three literal words before and after the quote, for a later Word insertion hook."""
    quote_words = " ".join(quote.split()).split()
    if not quote_words:
        return ABSENT
    body = re.sub(r"\[\[PÁGINA \d+\]\]", " ", contract_text)
    words = body.split()
    width = len(quote_words)
    start = -1
    for index in range(0, len(words) - width + 1):
        if words[index : index + width] == quote_words:
            start = index
            break
    if start < 0:
        return ABSENT
    before = words[max(0, start - 3) : start]
    after = words[start + width : start + width + 3]
    left = " ".join(before) if before else ABSENT
    right = " ".join(after) if after else ABSENT
    return f"antes: {left} | despues: {right}"


def _stamp_citations(risks: Any, contract_text: str) -> None:
    if not isinstance(risks, list):
        return
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        excerpt = " ".join(str(risk.get("excerpt") or "").split())
        page, section = _citation_context(excerpt, contract_text, str(risk.get("article") or ""))
        risk["pagina_exacta"] = page
        risk["seccion_articulo"] = section
        risk["cita_literal_exacta"] = excerpt[:500]
        risk["contexto_insercion_word"] = _word_anchor(excerpt[:500], contract_text)


def _citation_context(excerpt: str, contract_text: str, article: str) -> tuple[int, str]:
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


def _pages(contract_text: str) -> list[tuple[int, str]]:
    matches = list(_PAGE_MARK.finditer(contract_text))
    if not matches:
        return [(1, contract_text)]
    pages: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(contract_text)
        pages.append((int(match.group(1)), contract_text[match.start() : end]))
    return pages


def _map_collapsed(original: str, collapsed_index: int) -> int:
    compact = 0
    index = 0
    while index < len(original) and original[index].isspace():
        index += 1
    if collapsed_index <= 0:
        return index
    previous_space = False
    while index < len(original) and compact < collapsed_index:
        if original[index].isspace():
            if not previous_space:
                compact += 1
            previous_space = True
        else:
            compact += 1
            previous_space = False
        index += 1
    return index


def _drop_unanchored(risks: Any, contract_text: str) -> list[dict[str, Any]]:
    """Drop a card whose verbatim quote cannot be placed on a real page."""
    if not isinstance(risks, list):
        return []
    pages = _pages(contract_text)
    kept: list[dict[str, Any]] = []
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        quote = " ".join(str(risk.get("cita_literal_exacta") or risk.get("excerpt") or "").split())
        if len(quote) < 8 or not _excerpt_in_contract(quote, contract_text):
            continue
        page = risk.get("pagina_exacta")
        bodies = [body for number, body in pages if number == page]
        if bodies and not any(_excerpt_in_contract(quote, body) for body in bodies):
            continue
        risk["cita_literal_exacta"] = quote
        risk["contexto_insercion_word"] = _word_anchor(quote, contract_text)
        kept.append(risk)
    return kept


_PRIVACY_INTENT = re.compile(
    r"gdpr|rgpd|ccpa|datos personales|privacidad|data protection|protección de datos",
    re.IGNORECASE,
)
_COMPOUND_CUE = re.compile(r"capitaliz|compuest|compound|exponenc", re.IGNORECASE)
_FORUM_CUE = re.compile(
    r"ley aplicable|foro|jurisdicci[oó]n|governing law|sede arbitral|tribunal",
    re.IGNORECASE,
)


def _privacy_intent(text: str) -> bool:
    return bool(_PRIVACY_INTENT.search(text or ""))


def _map_note(reference_map: dict[str, Any] | None) -> str:
    if not reference_map:
        return ""
    return (
        "\n\nMAPA ESTRUCTURAL DEL PASE 1, solo en memoria. "
        "Cruce las cláusulas finales contra este mapa. No calcule importes. "
        "Si privacy_lock es true, no imponga un tribunal local sobre esa materia.\n"
        + json.dumps(reference_map, ensure_ascii=False)
    )


def build_reference_map(contract_text: str) -> dict[str, Any]:
    """Pass 1: early definitions, entities, and governing framework. Nothing is written to disk."""
    sections = _article_sections(contract_text)
    split_at = max(1, len(sections) // 2)
    early = sections[:split_at]
    entities: list[str] = []
    definitions: list[dict[str, str]] = []
    framework: list[dict[str, str]] = []
    privacy_lock = _privacy_intent(contract_text[:4000])
    for article, body in early:
        compact = " ".join(body.split())
        snippet = compact[:280]
        lowered = compact.lower()
        if any(word in lowered for word in ("parte", "comparec", "between", "acreedor", "deudor")):
            entities.append(snippet[:180])
        if any(word in lowered for word in ("definic", "definition", "significa", "means")):
            definitions.append({"article": article or ABSENT, "text": snippet})
        if _FORUM_CUE.search(compact):
            framework.append({"article": article or ABSENT, "text": snippet})
        if _privacy_intent(compact):
            privacy_lock = True
    return {
        "entities": entities[:6],
        "definitions": definitions[:8],
        "governing_framework": framework[:6],
        "privacy_lock": privacy_lock,
    }


def _extracted_number(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text.casefold() == ABSENT.casefold():
        return None
    match = re.search(r"\d[\d.,]*", text)
    if not match:
        return None
    return _parse_amount(match.group(0))


def calculate_exposure(
    base_fee: float | None,
    penalty_percentage: float | None,
    affected_years: float | None,
    compound: bool,
) -> str:
    """Cold arithmetic. The model is not allowed to supply this result."""
    if base_fee is None or penalty_percentage is None or affected_years is None:
        return ABSENT
    rate = penalty_percentage / 100.0
    if compound:
        amount = base_fee * (((1 + rate) ** affected_years) - 1)
        mode = "compuesto"
    else:
        amount = base_fee * rate * affected_years
        mode = "lineal"
    return f"{_fmt_usd(amount)} ({mode})"


def _apply_extracted_math(risks: Any) -> None:
    if not isinstance(risks, list):
        return
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        excerpt = str(risk.get("cita_literal_exacta") or risk.get("excerpt") or "")
        base_fee = _extracted_number(risk.get("base_fee"))
        penalty = _extracted_number(risk.get("penalty_percentage"))
        years = _extracted_number(risk.get("affected_years"))
        for key in ("objeto_afectado", "condicion_operativa", "base_fee", "penalty_percentage", "affected_years"):
            if not str(risk.get(key) or "").strip():
                risk[key] = ABSENT
        risk["cuantia_impacto_economico"] = calculate_exposure(
            base_fee,
            penalty,
            years,
            bool(_COMPOUND_CUE.search(excerpt)),
        )
        if risk["cuantia_impacto_economico"] != ABSENT or _short_condition(str(risk.get("condicion_operativa") or "")):
            risk["severity"] = "high"


def _short_condition(text: str) -> bool:
    match = re.search(r"(\d+)", text)
    if not match:
        return False
    count = int(match.group(1))
    lowered = text.lower()
    if "hora" in lowered or "hour" in lowered:
        return count < 120
    if "d" in lowered or "day" in lowered or "hábil" in lowered or "habil" in lowered:
        return count < 5
    return False


def _apply_reference_contradictions(
    payload: dict[str, Any],
    contract_text: str,
    reference_map: dict[str, Any] | None,
) -> None:
    """Pass 2: late indemnification and miscellaneous terms against the in-memory map."""
    if not reference_map:
        return
    risks = payload.get("risks")
    if not isinstance(risks, list):
        risks = []
        payload["risks"] = risks
    early_framework = " ".join(
        item.get("text", "") for item in reference_map.get("governing_framework", []) if isinstance(item, dict)
    )
    if not early_framework.strip():
        return
    sections = _article_sections(contract_text)
    late = sections[max(1, len(sections) // 2) :]
    for article, body in late:
        if reference_map.get("privacy_lock") and _privacy_intent(body):
            continue
        if not _FORUM_CUE.search(body):
            continue
        late_text = " ".join(body.split())
        if early_framework[:80].casefold() in late_text.casefold():
            continue
        excerpt = _verbatim(body, contract_text)
        if not excerpt:
            continue
        already = any(
            isinstance(risk, dict) and article and article in str(risk.get("article") or "")
            and "contradic" in f"{risk.get('title', '')} {risk.get('analysis', '')}".lower()
            for risk in risks
        )
        if already:
            continue
        risks.append(
            _backstop_risk(
                article or "cierre",
                "Contradicción entre el marco inicial y el cierre",
                excerpt,
                "La cláusula final no puede cumplirse junto con el marco de definiciones o de ley aplicable del inicio.",
                "Unifique la ley aplicable y el foro del cierre con las definiciones iniciales.",
                "La ley aplicable y el foro del cierre serán los mismos que fijan las definiciones iniciales.",
                origin="supervisor",
            )
        )


def _publish(
    payload: dict[str, Any],
    contract_text: str,
    reference_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload.pop("reasoning", None)
    if not isinstance(payload.get("metadata"), dict):
        payload["metadata"] = {}
    metadata = payload["metadata"]
    for key in ("parties", "contract_date", "fees", "term"):
        if not str(metadata.get(key) or "").strip():
            metadata[key] = ABSENT
    if not str(metadata.get("contract_type") or "").strip():
        metadata["contract_type"] = "No identificado"
    if not isinstance(payload.get("omissions"), list):
        payload["omissions"] = []
    payload["omissions"] = _drop_false_omissions(payload["omissions"], contract_text)
    _apply_reference_contradictions(payload, contract_text, reference_map)
    _apply_financial_controls(payload, contract_text)
    payload["risks"] = _ground_risks(payload.get("risks"), contract_text)
    _reinforce_matrix(payload, contract_text)
    payload["risks"] = _ground_risks(payload.get("risks"), contract_text)
    _stamp_citations(payload.get("risks"), contract_text)
    payload["risks"] = _drop_unanchored(payload.get("risks"), contract_text)
    _apply_extracted_math(payload.get("risks"))
    result = AuditResult.model_validate(payload)
    return result.model_dump()


def _keeps_desk_scope(risk: dict[str, Any]) -> bool:
    if risk.get("origin") != "litigador":
        return True
    blob = f"{risk.get('title', '')} {risk.get('analysis', '')}".lower()
    return any(
        word in blob
        for word in ("foro", "jurisdic", "sede", "ley aplicable", "arbitra", "tribunal", "juzgado", "venue", "governing law")
    )


def _risk_is_grounded(risk: dict[str, Any], contract_text: str) -> bool:
    excerpt = str(risk.get("excerpt") or "")
    article = str(risk.get("article") or "")
    if len(excerpt) < 8 or not _excerpt_in_contract(excerpt, contract_text):
        return False
    numbers = _ARTICLE_NUM.findall(article)
    if not numbers:
        return False
    return all(number in contract_text for number in numbers)


def _excerpt_in_contract(excerpt: str, contract_text: str) -> bool:
    if excerpt in contract_text:
        return True
    return " ".join(excerpt.split()) in " ".join(contract_text.split())


def _drop_false_omissions(omissions: list[Any], contract_text: str) -> list[Any]:
    lowered = contract_text.lower()
    kept: list[Any] = []
    for item in omissions:
        if not isinstance(item, dict):
            continue
        topic = f"{item.get('clause', '')} {item.get('analysis', '')}".lower()
        if _topic_is_present(topic, lowered):
            continue
        if not str(item.get("article") or "").strip():
            item["article"] = "Índice revisado"
        kept.append(item)
    return kept


def _topic_is_present(topic: str, contract_lower: str) -> bool:
    checks = (
        ("confid", ("confidential", "confidencial", "non-disclosure", "no divulg", "información reservada", "informacion reservada")),
        ("secreto", ("confidential", "confidencial", "información secreta", "informacion secreta")),
        ("indemn", ("indemnif", "indemniz", "hold harmless")),
        ("controv", ("jurisdic", "jurisdiction", "arbitra", "dispute", "foro", "venue", "controvers")),
        ("jurisdic", ("jurisdic", "jurisdiction", "arbitra", "dispute", "foro", "venue")),
        ("foro", ("jurisdic", "jurisdiction", "arbitra", "dispute", "foro", "venue")),
        ("fuerza mayor", ("fuerza mayor", "force majeure", "caso fortuito")),
        ("terminac", ("termination", "terminación", "terminacion", "rescisión", "rescision")),
    )
    for needle, markers in checks:
        if needle in topic and any(marker in contract_lower for marker in markers):
            return True
    return False


def _load_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
        text = text.strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("Audit payload must be a JSON object.")
    return payload
