"""One-schema OpenAI extraction at temperature 0.0.

Each call sends a single clause and one Pydantic schema. The return value is
the parsed JSON plus call metadata. This module does not score risk or rewrite
extracted fields.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TypedDict

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

from extractors_schema import (
    JurisdictionDisputesSchema,
    PaymentPenaltySchema,
    PrivacyComplianceSchema,
)

MODEL = "gpt-4o"
TEMPERATURE = 0.0

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
_SCHEMAS: dict[type[BaseModel], str] = {
    PaymentPenaltySchema: "payment",
    PrivacyComplianceSchema: "privacy",
    JurisdictionDisputesSchema: "jurisdiction",
}
_SYSTEM = (
    "Extract only explicit statements from the clause into the schema. "
    "Copy quotes verbatim. Use null or an empty list when a variable is not stated, "
    "and set the faltante flag to match that absence. "
    "Do not calculate risk, damages, or any value the clause does not write."
)


class ExtractionResult(TypedDict):
    extracted: dict[str, object]
    verification: dict[str, object]


def _load_api_key() -> str:
    """Read OPENAI_API_KEY from the environment, then the repo-root .env file."""
    load_dotenv(_ENV_FILE, override=False)
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key or key.startswith("sk-your-"):
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    return key


def _client() -> OpenAI:
    return OpenAI(api_key=_load_api_key())


def run_strict_extraction(
    clause_text: str,
    schema_type: type[BaseModel],
) -> ExtractionResult:
    """Call gpt-4o once, locked to one schema and temperature 0.0."""
    if schema_type not in _SCHEMAS:
        raise TypeError(
            "schema_type must be PaymentPenaltySchema, PrivacyComplianceSchema, "
            "or JurisdictionDisputesSchema."
        )
    if not clause_text or not clause_text.strip():
        raise ValueError("clause_text is empty.")

    # chat.completions.parse is the Structured Outputs entry in openai 1.109.
    # It sends the Pydantic model as a strict json_schema and parses the reply
    # back into that model. json_object mode does not enforce the schema.
    completion = _client().chat.completions.parse(
        model=MODEL,
        temperature=TEMPERATURE,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": clause_text},
        ],
        response_format=schema_type,
    )
    choice = completion.choices[0]
    message = choice.message
    if message.parsed is None:
        raise RuntimeError(message.refusal or "The model returned no schema object.")

    return {
        "extracted": message.parsed.model_dump(),
        "verification": {
            "model": MODEL,
            "temperature": TEMPERATURE,
            "schema": schema_type.__name__,
            "layer": _SCHEMAS[schema_type],
            "response_id": completion.id,
            "finish_reason": choice.finish_reason,
            "validated": True,
        },
    }


_SAMPLES: dict[str, tuple[type[BaseModel], str]] = {
    "payment": (
        PaymentPenaltySchema,
        "clause_id 4. The Client shall pay a penalty of 5% of the monthly retainer.",
    ),
    "privacy": (
        PrivacyComplianceSchema,
        "Personal data may be transferred outside the EEA under the GDPR and the CCPA.",
    ),
    "jurisdiction": (
        JurisdictionDisputesSchema,
        "This agreement is governed by the laws of Costa Rica. "
        "Disputes shall be resolved by the courts of San Jose.",
    ),
}


def main(argv: list[str] | None = None) -> ExtractionResult:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in _SAMPLES:
        names = ", ".join(_SAMPLES)
        raise SystemExit(f"usage: python extractor_runner.py [{names}] [clause text]")
    schema_type, sample = _SAMPLES[args[0]]
    clause_text = sample if len(args) == 1 else " ".join(args[1:])
    result = run_strict_extraction(clause_text, schema_type)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
