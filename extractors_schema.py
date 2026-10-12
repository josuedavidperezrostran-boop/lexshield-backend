"""Strict schemas for the specialized extraction layer.

Three independent models: penalties, privacy, and jurisdiction. Each schema
owns only its own keys, so one layer cannot validate another layer's payload.
Declarative only. No client calls and no FastAPI.

When a schema is later attached to OpenAI Structured Outputs, that call runs
at temperature 0.0.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _require_all_fields(schema: dict) -> None:
    """Publish every key as required for Structured Outputs."""
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["required"] = list(properties)
    schema["additionalProperties"] = False


_STRICT = ConfigDict(extra="forbid", strict=True, json_schema_extra=_require_all_fields)


class PaymentPenaltySchema(BaseModel):
    """Penalty variables for one indexed clause, or an explicit absence."""

    model_config = _STRICT

    valor_multa: str | None = Field(
        description=(
            "Explicit percentage, rate, or monetary fee stated in the clause. "
            "Null when the clause does not state one, and null whenever faltante is true."
        ),
    )
    base_calculo: str | None = Field(
        description=(
            "Explicit financial pool the penalty applies to, such as "
            '"5-year gross revenue" or "monthly retainer". '
            "Null when the clause does not state one, and null whenever faltante is true."
        ),
    )
    clause_id: str = Field(
        description="Unique index string passed from the indexer.",
    )
    cita_literal: str = Field(
        description="Exact verbatim quote from the clause that corroborates this extraction.",
    )
    faltante: bool = Field(
        description=(
            "True only when the clause states no penalty variables. "
            "valor_multa and base_calculo are then null."
        ),
    )

    @model_validator(mode="after")
    def bind_faltante(self) -> PaymentPenaltySchema:
        """Reject a faltante flag that disagrees with the penalty fields."""
        if not self.clause_id.strip():
            raise ValueError("clause_id must be the indexer index string.")
        for name in ("valor_multa", "base_calculo"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"{name} must be an explicit string or null.")

        stated = self.valor_multa is not None or self.base_calculo is not None
        if self.faltante and stated:
            raise ValueError(
                "faltante is true, so valor_multa and base_calculo must be null."
            )
        if not self.faltante and not stated:
            raise ValueError(
                "faltante is false, so valor_multa or base_calculo must be stated."
            )
        if not self.faltante and not self.cita_literal.strip():
            raise ValueError(
                "cita_literal must quote the clause when a penalty variable is present."
            )
        return self


PenaltyExtraction = PaymentPenaltySchema


class PrivacyComplianceSchema(BaseModel):
    """Privacy terms found in one clause, or an explicit absence."""

    model_config = _STRICT

    normativas_detectadas: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit regulations named in the clause, such as GDPR or CCPA. "
            "Empty list when none are named."
        ),
    )
    transferencia_internacional: bool = Field(
        description=(
            "True only when the clause explicitly regulates cross-border data transfer. "
            "False when it does not, including when faltante_privacidad is true."
        ),
    )
    cita_literal_privacidad: str | None = Field(
        description=(
            "Verbatim quote that proves the privacy intent. "
            "Null when the clause has no privacy quote, and null when faltante_privacidad is true."
        ),
    )
    faltante_privacidad: bool = Field(
        description=(
            "True only when the clause states no data-privacy terms. "
            "The regulation list is then empty, cross-border transfer is false, "
            "and the quote is null."
        ),
    )

    @model_validator(mode="after")
    def bind_faltante_privacidad(self) -> PrivacyComplianceSchema:
        """Reject a privacy flag that disagrees with the recorded terms."""
        for name in self.normativas_detectadas:
            if not name.strip():
                raise ValueError(
                    "normativas_detectadas entries must be explicit regulation names."
                )
        if self.cita_literal_privacidad is not None and not self.cita_literal_privacidad.strip():
            raise ValueError("cita_literal_privacidad must be a verbatim quote or null.")

        stated = (
            bool(self.normativas_detectadas)
            or self.transferencia_internacional
            or self.cita_literal_privacidad is not None
        )
        if self.faltante_privacidad and stated:
            raise ValueError(
                "faltante_privacidad is true, so no privacy terms may be recorded."
            )
        if not self.faltante_privacidad and not stated:
            raise ValueError(
                "faltante_privacidad is false, so a privacy term must be recorded."
            )
        return self


class JurisdictionDisputesSchema(BaseModel):
    """Governing law and forum found in one clause, or an explicit absence."""

    model_config = _STRICT

    ley_aplicable: str | None = Field(
        description=(
            "Explicit governing-law state or country, such as Costa Rica or Delaware. "
            "Null when the clause does not name one, and null when faltante_disputas is true."
        ),
    )
    sede_arbitraje_tribunales: str | None = Field(
        description=(
            "Literal location where a legal conflict is resolved. "
            "Null when the clause does not name one, and null when faltante_disputas is true."
        ),
    )
    cita_literal_disputas: str | None = Field(
        description=(
            "Verbatim quote that proves the jurisdictional forum. "
            "Null when the clause has no dispute quote, and null when faltante_disputas is true."
        ),
    )
    faltante_disputas: bool = Field(
        description=(
            "True only when the clause states no dispute terms. "
            "Governing law, forum, and quote are then null."
        ),
    )

    @model_validator(mode="after")
    def bind_faltante_disputas(self) -> JurisdictionDisputesSchema:
        """Reject a dispute flag that disagrees with the recorded forum."""
        for name in ("ley_aplicable", "sede_arbitraje_tribunales", "cita_literal_disputas"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"{name} must be explicit text or null.")

        stated = any(
            value is not None
            for value in (
                self.ley_aplicable,
                self.sede_arbitraje_tribunales,
                self.cita_literal_disputas,
            )
        )
        if self.faltante_disputas and stated:
            raise ValueError(
                "faltante_disputas is true, so law, forum, and quote must be null."
            )
        if not self.faltante_disputas and not stated:
            raise ValueError(
                "faltante_disputas is false, so a dispute term must be recorded."
            )
        return self
