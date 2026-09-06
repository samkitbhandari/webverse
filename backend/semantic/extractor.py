"""The semantic compiler.

Turns a chunk of a source into candidate knowledge: entities, claims,
constraints, decisions, events, risks and the relationships between them.

Two things make this more than "call an LLM with a JSON schema":

1. **Grounding.** The prompt carries the entities and claims the graph already
   holds for this neighbourhood, retrieved by embedding similarity. Without it
   the model invents a fresh name for an entity it has already seen and the
   graph fragments into synonyms.
2. **A floor.** If the provider is offline, rate-limited or returns unparseable
   output, extraction falls through to the deterministic rules rather than
   failing. The pipeline never stalls on a provider problem.
"""
from __future__ import annotations

import logging
from typing import Iterable, Sequence

from pydantic import ValidationError

from backend.core.models import Extraction
from backend.semantic import rules
from backend.semantic.llm import get_llm

log = logging.getLogger("nexus.extractor")

SYSTEM_PROMPT = """\
You are the semantic compiler of NEXUS Omega, a knowledge intelligence engine.

You convert a passage of a document into structured knowledge. You do not
summarise and you do not speculate. Every item you emit must be traceable to
something the passage actually states.

The ontology:
- entity      a concrete thing: organisation, system, component, project, \
regulation, person, location
- claim       a proposition asserting a value for a property of an entity
- constraint  a limit an entity must satisfy (has an operator and a threshold)
- decision    a choice that was made or committed to
- event       something that changed the world (a publication, a revision, a \
failure, a deprecation)
- risk        a named potential negative outcome
- relationship a typed link between two items you emitted

Rules you must follow:
- Claims and constraints MUST be decomposed into (subject, predicate, value).
  The subject is an entity name. The predicate is the property, expressed in
  lowercase words, e.g. "max operating temperature", "capacity", "latency",
  "cost". Reuse a predicate spelling from KNOWN PREDICATES when one fits: two
  documents describing the same property must produce the same predicate
  string, or the system cannot tell they are talking about the same thing.
- Reuse entity names from KNOWN ENTITIES verbatim when the passage refers to
  the same thing. Only invent a name when the entity is genuinely new.
- Put the number and its unit in "value" and "unit" separately when you can.
- confidence is how strongly the PASSAGE asserts the item (0.0-1.0), not how
  plausible you find it. Hedged language ("expected to", "approximately")
  lowers it; a measured figure in a table raises it.
- valid_from / valid_until: only when the passage states or clearly implies a
  date. ISO-8601. Otherwise null.
- Emit nothing you cannot ground in the passage. An empty list is a valid and
  frequently correct answer.
"""

SCHEMA_HINT = """\
Respond with exactly this JSON shape:

{
  "entities":      [{"name": str, "kind": str, "description": str, "aliases": [str]}],
  "claims":        [{"text": str, "subject": str, "predicate": str, "value": str,
                     "unit": str|null, "confidence": float,
                     "valid_from": str|null, "valid_until": str|null,
                     "kind": "Claim"|"Observation"|"Assumption"}],
  "constraints":   [{"text": str, "subject": str, "predicate": str,
                     "operator": "LT"|"LTE"|"GT"|"GTE"|"EQ"|"NEQ",
                     "value": str, "unit": str|null,
                     "kind": "Constraint"|"Requirement", "confidence": float}],
  "decisions":     [{"text": str, "subject": str, "rationale": str,
                     "based_on": [str], "confidence": float}],
  "events":        [{"text": str, "subject": str, "occurred_at": str|null,
                     "affects": [str], "confidence": float}],
  "risks":         [{"text": str, "subject": str, "severity": float, "confidence": float}],
  "relationships": [{"source": str, "target": str,
                     "type": "SUPPORTS"|"CONTRADICTS"|"DEPENDS_ON"|"REQUIRES"|
                             "CAUSES"|"AFFECTS"|"IMPLEMENTS"|"SUPERSEDES"|
                             "DERIVED_FROM"|"OWNED_BY"|"CONSTRAINS"|"INVALIDATES"|
                             "RELATED_TO"|"BASED_ON"|"ABOUT",
                     "rationale": str, "weight": float, "confidence": float}]
}
"""


def build_user_prompt(
    text: str,
    *,
    source_label: str = "",
    known_entities: Sequence[str] = (),
    known_predicates: Sequence[str] = (),
    related_claims: Sequence[str] = (),
    default_subject: str | None = None,
) -> str:
    parts: list[str] = []
    if source_label:
        parts.append(f"SOURCE: {source_label}")
    if default_subject:
        parts.append(
            f"PRINCIPAL SUBJECT OF THIS DOCUMENT: {default_subject}\n"
            "Where a sentence states a property without naming what it belongs "
            "to, attribute it to this subject."
        )
    if known_entities:
        parts.append("KNOWN ENTITIES (reuse these names verbatim where they apply):\n"
                     + "\n".join(f"- {e}" for e in known_entities[:40]))
    if known_predicates:
        parts.append("KNOWN PREDICATES (reuse where they fit):\n"
                     + "\n".join(f"- {p}" for p in known_predicates[:30]))
    if related_claims:
        parts.append(
            "RELATED KNOWLEDGE ALREADY IN THE GRAPH (for grounding; do not "
            "re-emit unless the passage restates it):\n"
            + "\n".join(f"- {c}" for c in related_claims[:15])
        )
    parts.append("PASSAGE:\n" + text.strip())
    return "\n\n".join(parts)


def compile_chunk(
    text: str,
    *,
    source_label: str = "",
    known_entities: Sequence[str] = (),
    known_predicates: Sequence[str] = (),
    related_claims: Sequence[str] = (),
    default_subject: str | None = None,
) -> tuple[Extraction, str]:
    """Compile one chunk. Returns (extraction, extractor_name)."""
    if not text or not text.strip():
        return Extraction(), "empty"

    llm = get_llm()
    if llm.name != "offline":
        user = build_user_prompt(
            text,
            source_label=source_label,
            known_entities=known_entities,
            known_predicates=known_predicates,
            related_claims=related_claims,
            default_subject=default_subject,
        )
        raw = llm.complete_json(SYSTEM_PROMPT, user, SCHEMA_HINT)
        if raw:
            try:
                ex = Extraction.model_validate(_coerce(raw))
                if not ex.is_empty():
                    return ex, llm.name
                log.info("provider returned an empty extraction; using rules")
            except ValidationError as exc:
                log.warning("provider output failed validation (%s); using rules",
                            str(exc).splitlines()[0])

    return rules.extract(text, known_entities=known_entities,
                         source_label=source_label,
                         default_subject=default_subject), "rules"


def _coerce(raw: dict) -> dict:
    """Tolerate the small shape deviations models make.

    Wrong-but-recoverable output is common enough -- a bare list, a singular
    key, a null where a list belongs -- that discarding a whole extraction over
    it wastes a real API call.
    """
    if not isinstance(raw, dict):
        return {}
    alias = {
        "entity": "entities", "claim": "claims", "constraint": "constraints",
        "requirements": "constraints", "requirement": "constraints",
        "decision": "decisions", "event": "events", "risk": "risks",
        "relationship": "relationships", "relations": "relationships",
        "edges": "relationships", "observations": "claims",
    }
    out: dict[str, list] = {}
    for key, value in raw.items():
        k = alias.get(key.lower(), key.lower())
        if value is None:
            continue
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list):
            continue
        out.setdefault(k, []).extend(v for v in value if isinstance(v, dict))

    # constraints arriving as requirements keep their kind
    for c in out.get("constraints", []):
        c.setdefault("kind", "Constraint")
        if isinstance(c.get("value"), (int, float)):
            c["value"] = str(c["value"])
    for c in out.get("claims", []):
        if isinstance(c.get("value"), (int, float)):
            c["value"] = str(c["value"])
        c.setdefault("text", c.get("subject", ""))
    return {k: v for k, v in out.items() if k in Extraction.model_fields}


def summarise_change(prompt: str) -> str:
    """Optional prose narration of a change, used by the explainability layer."""
    llm = get_llm()
    if llm.name == "offline":
        return ""
    return llm.complete_text(
        "You explain knowledge changes to engineers. Be concrete and brief. "
        "Never invent facts that are not in the input.",
        prompt,
        max_tokens=350,
    ).strip()
