"""Deterministic semantic extraction.

A rule-based compiler from prose to the knowledge model. It exists for two
reasons: the demo must run with no API key and no network, and every LLM
extraction needs a floor to be compared against.

It is a pattern extractor, not a parser, and it is honest about that -- the
confidences it emits are lower than an LLM's, and everything it produces is
tagged ``extractor="rules"`` in provenance so the UI can show where a belief
came from. What it does reliably is the shape of statement this domain is full
of: "X must remain below N unit", "X capacity is N unit", "reduced from A to B".
"""
from __future__ import annotations

import re
from typing import Iterable

from backend.core.models import (
    EdgeType,
    ExtractedClaim,
    ExtractedConstraint,
    ExtractedDecision,
    ExtractedEntity,
    ExtractedEvent,
    ExtractedRelationship,
    ExtractedRisk,
    Extraction,
)

# --- vocabulary ------------------------------------------------------------
_COMPARATORS: list[tuple[str, str]] = [
    (r"must not exceed|shall not exceed|may not exceed|not exceed|no more than|"
     r"at most|up to a maximum of|capped at", "LTE"),
    (r"must remain below|must stay below|must be below|must be under|"
     r"must be less than|less than|below|under|beneath", "LT"),
    (r"at least|no less than|not less than|minimum of|no lower than", "GTE"),
    (r"must exceed|greater than|more than|above|over", "GT"),
    (r"must equal|must be exactly|exactly", "EQ"),
]

_REQUIREMENT_CUES = re.compile(
    r"\b(must|shall|required to|requirement|mandat\w*|obliged|may not|"
    r"is limited to|limit of|budget|not exceed)\b", re.I
)
_DECISION_CUES = re.compile(
    r"\b(decision|we (?:will|have|shall)|decided|selected|chose|chosen|approved|"
    r"opted|go(?:ing)? with|adopt(?:ed)?|award(?:ed)? to)\b", re.I
)
_EVENT_CUES = re.compile(
    r"\b(published|issued|announced|updated|revised|amended|deprecat\w*|"
    r"effective (?:from|as of)|withdrawn|recalled|reduced from|increased from|"
    r"came into force|takes effect|superseded?)\b", re.I
)
_RISK_CUES = re.compile(
    r"\b(risk|hazard|may fail|could fail|could result in|threatens?|exposure|"
    r"vulnerab\w*|jeopardis\w*|jeopardiz\w*|danger)\b", re.I
)
_OBSERVATION_CUES = re.compile(
    r"\b(measured|observed|recorded|logged|telemetry|test(?:ing|ed)? show\w*|"
    r"reported at|sampled|monitored)\b", re.I
)
_ASSUMPTION_CUES = re.compile(
    r"\b(assum\w+|presum\w+|expected to|anticipat\w+|baseline of)\b", re.I
)

#: "X is now deprecated" asserts a value just as firmly as "X is 60 degC".
#: Without this the extractor emits nothing for polarity statements, and
#: SEMANTIC contradictions -- one of the four kinds the system detects -- are
#: undemonstrable unless an LLM is configured.
_POLARITY_RE = re.compile(
    r"\b(?:is|are|was|were|remains?|becomes?|has been|have been)\s+"
    r"(?:now\s+|being\s+)?"
    r"(?P<neg>no longer\s+|not\s+|never\s+)?"
    r"(?:now\s+|being\s+)?"
    r"(?P<state>deprecated|withdrawn|obsolete|discontinued|unsupported|"
    r"non-compliant|noncompliant|decertified|revoked|suspended|unavailable|"
    r"supported|certified|approved|compliant|active|operational|available)\b",
    re.I,
)

#: Negation inverts the assertion, so "no longer compliant" must land on the
#: same value as "non-compliant" -- otherwise the two phrasings would not be
#: recognised as saying the same thing, and a document saying a thing has
#: lapsed would be recorded as saying it holds.
_POLARITY_OPPOSITE: dict[str, str] = {
    "supported": "unsupported", "unsupported": "supported",
    "certified": "decertified", "decertified": "certified",
    "approved": "revoked", "revoked": "approved",
    "compliant": "non-compliant", "non-compliant": "compliant",
    "noncompliant": "compliant",
    "active": "suspended", "suspended": "active",
    "operational": "unavailable", "available": "unavailable",
    "unavailable": "available",
    "deprecated": "supported", "withdrawn": "available",
    "obsolete": "supported", "discontinued": "supported",
}

#: Which property each state is asserting something about. Two documents must
#: land on the same predicate for their disagreement to be detectable at all.
_POLARITY_PREDICATE: dict[str, str] = {
    "deprecated": "support status", "withdrawn": "support status",
    "obsolete": "support status", "discontinued": "support status",
    "unsupported": "support status", "supported": "support status",
    "decertified": "certification status", "revoked": "certification status",
    "suspended": "certification status", "certified": "certification status",
    "approved": "certification status",
    "non-compliant": "compliance status", "noncompliant": "compliance status",
    "compliant": "compliance status",
    "unavailable": "availability status", "available": "availability status",
    "active": "availability status", "operational": "availability status",
}

#: Surface forms -> the canonical predicate two documents must agree on.
PREDICATE_SYNONYMS: list[tuple[re.Pattern[str], str]] = [
    # Ambient is checked first: it is a property of the environment, not of the
    # equipment. Folding it into "max operating temperature" made a 41 degC
    # weather reading contradict a 68 degC pack measurement.
    (re.compile(r"ambient temperature|outside temperature|air temperature", re.I),
     "ambient temperature"),
    (re.compile(r"operating temperature|temperature limit|temperature threshold|"
                r"thermal limit|max\w* temperature|cell temperature|"
                r"pack temperature", re.I),
     "max operating temperature"),
    (re.compile(r"charg\w+ (?:time|duration)", re.I), "charging time"),
    (re.compile(r"charg\w+ (?:power|rate)", re.I), "charging power"),
    (re.compile(r"\b(?:production |monthly |manufacturing |supply )?capacity\b|"
                r"throughput per month|output per month", re.I), "capacity"),
    (re.compile(r"latency|response time|round[- ]trip time", re.I), "latency"),
    (re.compile(r"throughput|transactions per second|requests per second", re.I),
     "throughput"),
    (re.compile(r"\b(?:unit |total |capital |operating )?cost\b|price|budget|"
                r"expenditure|capex|opex", re.I), "cost"),
    (re.compile(r"\brange\b|driving range|distance per charge", re.I), "range"),
    (re.compile(r"availability|uptime|service level", re.I), "availability"),
    (re.compile(r"lead time|delivery time|turnaround", re.I), "lead time"),
    (re.compile(r"energy density", re.I), "energy density"),
    (re.compile(r"cycle life|service life|lifetime", re.I), "cycle life"),
    (re.compile(r"weight|mass", re.I), "mass"),
    (re.compile(r"utilisation|utilization|load factor", re.I), "utilisation"),
    (re.compile(r"fleet size|number of vehicles|vehicle count", re.I), "fleet size"),
]

#: All-caps tokens that the entity pattern would otherwise happily collect.
#: "3120 INR" made "INR" an entity, and then discourse continuity attributed
#: the next sentence's value to it -- producing claims like "INR cost = 2,740".
_UNIT_TOKENS = {
    "INR", "USD", "EUR", "GBP", "JPY", "AED", "CNY",
    "TPS", "RPS", "QPS", "KW", "MW", "KWH", "MWH", "WH", "AH", "VDC", "VAC",
    "MS", "KM", "CM", "MM", "KG", "HZ", "KHZ", "MHZ", "GHZ", "PSI", "RPM",
    "AC", "DC", "OK", "NA", "TBD", "TBC", "ETA", "FYI",
}

_STOPWORD_STARTS = {
    "The", "This", "That", "These", "Those", "A", "An", "It", "We", "They",
    "However", "Therefore", "Because", "Since", "If", "When", "While", "As",
    "After", "Before", "Following", "Under", "For", "In", "On", "At", "All",
    "Each", "Any", "Both", "Its", "Their", "Our", "There", "Per",
}

# A number must not be glued to a letter: "A7", "BP-7" and "R100" are entity
# names, not quantities, and matching the digits inside them was the single
# largest source of junk claims.
_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9-])"
    r"(?P<cur>[₹$€])?\s*"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s*(?P<mag>lakhs?|lac|crores?|million|billion|thousand|mn|bn|k)\b)?"
    r"(?:\s*(?P<unit>%|percent\b|pct\b|"
    r"°\s?[CF]|deg\s?[CF]|degrees? (?:celsius|centigrade|fahrenheit)|[CF]\b|"
    r"INR\b|USD\b|EUR\b|GBP\b|JPY\b|rupees?\b|dollars?\b|"      # written currencies
    r"kWh|kW|MWh|MW|Wh|ms\b|msec\b|secs?\b|seconds?\b|minutes?\b|mins?\b|"
    r"hours?\b|hrs?\b|days?\b|weeks?\b|months?\b|km\b|cm\b|mm\b|kg\b|tonnes?\b|"
    r"TPS\b|rps\b|qps\b|V\b|Ah\b|"
    r"units?(?:\s*(?:/|per)\s*\w+)?|vehicles?|users?|cells?|stations?|pieces?))?",
    re.I,
)

#: When the phrasing gives no predicate, the unit usually does. A figure in
#: degC is a temperature whether or not the sentence says so.
_UNIT_PREDICATE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(°|deg|degree)|^[CF]$", re.I), "max operating temperature"),
    (re.compile(r"^(ms|msec|sec|second|min|minute|hour|hr|day|week)", re.I), "duration"),
    (re.compile(r"^(tps|rps|qps)$", re.I), "throughput"),
    (re.compile(r"^(km|cm|mm)$", re.I), "range"),
    (re.compile(r"^(kwh|kw|mwh|mw|wh)$", re.I), "power"),
    (re.compile(r"^(kg|tonne)", re.I), "mass"),
    (re.compile(r"^(unit|piece|vehicle|cell|station)", re.I), "capacity"),
    (re.compile(r"^%$"), "percentage"),
]

_CURRENCY_PREDICATE = "cost"

_ENTITY_RE = re.compile(
    r"\b(?:[A-Z][a-zA-Z]*(?:[- ][A-Z0-9][a-zA-Z0-9-]*)+|"      # Multi-word Proper Noun
    r"[A-Z]{2,}(?:[- ]?\d+(?:\.\d+)?)?|"                        # UNECE, R100, ISO 26262
    r"[A-Z][a-zA-Z]+-\d+|"                                      # BP-7
    r"[A-Z]\d+)\b"                                              # A7
)

_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9])")


_HEADING_LINE = re.compile(r"^\s*#{1,6}\s|^\s*[-*=_]{3,}\s*$")


def split_sentences(text: str) -> list[str]:
    """Sentences worth extracting from.

    Two things this must get right:

    **Headings are excluded.** Chunks carry their heading path for context
    ("## Spec > Validation"), which helps a language model and actively harms a
    rule extractor: the heading is full of capitalised words, so it wins subject
    selection and every claim in the section gets attributed to the document
    title instead of the entity the sentence is about.

    **Hard wrapping is not a sentence boundary.** Markdown prose is routinely
    wrapped at 80 columns, and splitting on the newline cuts
    "reduced the limit from / 70 degC to 60 degC" in half -- which turns a
    change statement into a claim asserting the *old* value. Lines are
    therefore reflowed into paragraphs before sentence splitting.
    """
    paragraphs: list[str] = []
    buf: list[str] = []
    for line in (text or "").splitlines():
        if _HEADING_LINE.match(line) or not line.strip():
            if buf:
                paragraphs.append(" ".join(buf))
                buf = []
            continue
        buf.append(line.strip())
    if buf:
        paragraphs.append(" ".join(buf))

    out: list[str] = []
    for para in paragraphs:
        for raw in _SENTENCE_RE.split(para):
            s = " ".join(raw.split())
            if len(s) > 8:
                out.append(s)
    return out


def canonical_predicate(phrase: str) -> str | None:
    for pattern, canon in PREDICATE_SYNONYMS:
        if pattern.search(phrase):
            return canon
    return None


def _fallback_predicate(left: str) -> str:
    """Last-resort predicate: the head noun phrase before the comparator."""
    words = re.findall(r"[a-z][a-z-]+", left.lower())
    words = [w for w in words if w not in {
        "the", "a", "an", "of", "for", "to", "is", "are", "was", "were", "be",
        "must", "shall", "will", "has", "have", "had", "its", "their", "this",
        "that", "at", "in", "on", "by", "with", "and", "or", "not", "per",
        "system", "we", "it", "each", "any", "all", "may", "can", "should",
    }]
    return " ".join(words[-3:]) if words else "value"


_DATEISH = re.compile(
    r"^(quarter|q[1-4]|h[12]|fy|week|month|year|phase)\s*\d*$|^\d{4}$", re.I
)


def _distinctive_reference(text: str, known: Iterable[str]) -> str | None:
    """Resolve a shorthand mention like "pack temperature" to its full entity.

    Reports rarely repeat the full name: a document introduces "Battery Pack
    BP-7" and then writes "measured pack temperature". Without this, those
    sentences carry no entity at all and the value gets attributed to whichever
    proper noun appeared most recently -- which is how a corridor ended up
    owning a battery's temperature.

    A token only resolves when it belongs to exactly one known entity, so an
    ambiguous word like "fleet" (Phase 1 and Phase 2 both contain it) is left
    alone rather than guessed at.
    """
    words = set(re.findall(r"[a-z][a-z0-9-]{2,}", text.lower()))
    if not words:
        return None

    owners: dict[str, set[str]] = {}
    for name in known:
        for token in re.findall(r"[a-z][a-z0-9-]{2,}", name.lower()):
            if token in _STOP_TOKENS:
                continue
            owners.setdefault(token, set()).add(name)

    for token in words:
        candidates = owners.get(token)
        if candidates and len(candidates) == 1:
            return next(iter(candidates))
    return None


#: Words too generic to identify an entity by themselves.
_STOP_TOKENS = {
    "the", "and", "of", "ltd", "limited", "inc", "corp", "systems", "system",
    "energy", "cells", "cell", "north", "south", "east", "west", "phase",
    "deployment", "authority", "programme", "program", "urban", "mobility",
    "transit", "hub", "type", "thermal", "architecture", "battery",
}


def find_entities(text: str, known: Iterable[str] = ()) -> list[str]:
    """Proper-noun-ish spans, plus any known entity mentioned verbatim."""
    found: list[str] = []
    seen: set[str] = set()

    for name in known:
        if name and re.search(rf"\b{re.escape(name)}\b", text, re.I):
            k = name.lower()
            if k not in seen:
                seen.add(k)
                found.append(name)

    for m in _ENTITY_RE.finditer(text):
        cand = m.group(0).strip()
        first = cand.split()[0]
        if first in _STOPWORD_STARTS and len(cand.split()) < 3:
            continue
        if cand.upper() in _UNIT_TOKENS:
            continue
        if _DATEISH.match(cand):
            continue          # "Quarter 2" is a reporting period, not a thing
        if len(cand) < 3 or cand.lower() in seen:
            continue
        seen.add(cand.lower())
        found.append(cand)
    return found


def _pick_subject(sentence: str, known: Iterable[str]) -> str | None:
    ents = find_entities(sentence, known)
    return ents[0] if ents else None


def _split_on_comparator(sentence: str) -> tuple[str, str, str] | None:
    """(left_of_comparator, operator, right_of_comparator)."""
    for pattern, op in _COMPARATORS:
        m = re.search(pattern, sentence, re.I)
        if m:
            return sentence[: m.start()], op, sentence[m.end():]
    return None


def _value_in(text: str) -> tuple[str, str | None] | None:
    """Return (number_with_magnitude, unit) -- never the unit twice.

    "50,000 units per month" -> ("50,000", "units per month")
    "20 lakh"                -> ("20 lakh", None)
    "Phase 2"                -> None            (no unit: not a quantity)
    """
    for m in _VALUE_RE.finditer(text):
        if not m.group("num"):
            continue
        unit = (m.group("unit") or "").strip() or None
        cur = m.group("cur")
        mag = m.group("mag")
        if not unit and cur:
            unit = cur
        # A bare integer with neither unit nor magnitude is almost always an
        # identifier or an ordinal ("Phase 2", "rev.3"), not a measurement.
        if not unit and not mag:
            continue
        number = m.group("num") + (f" {mag}" if mag else "")
        return number.strip(), unit
    return None


def _predicate_from_unit(unit: str | None, value_text: str = "") -> str | None:
    if unit:
        for pattern, canon in _UNIT_PREDICATE:
            if pattern.search(unit.strip()):
                return canon
    if re.search(r"[₹$€]", f"{unit or ''}{value_text}"):
        return _CURRENCY_PREDICATE
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def extract(
    text: str,
    known_entities: Iterable[str] = (),
    source_label: str = "",
    default_subject: str | None = None,
) -> Extraction:
    """Compile a chunk of prose into candidate knowledge.

    ``default_subject`` is the document's principal subject, supplied by the
    caller. Sections like "## Validation -- measured pack temperature reached
    68 degC" name no entity at all: the entity was established pages earlier
    and the reader carries it. Without that fallback the measurement is
    silently dropped, which is the worst outcome of the three. Claims resting
    on it are emitted at reduced confidence and flagged, not passed off as
    directly attributed.
    """
    known = list(known_entities)
    ex = Extraction()
    entity_names: dict[str, str] = {}
    #: Discourse continuity. "Measured pack temperature reached 68 degC" names
    #: no entity, but the previous sentence did, and dropping the sentence for
    #: want of a subject loses real observations. It decays after two
    #: sentences -- carried indefinitely it starts attributing statements to
    #: whatever entity was named furthest back, which is worse than a miss.
    current_subject: str | None = None
    carry = 0
    CARRY_LIMIT = 2

    for sentence in split_sentences(text):
        for name in find_entities(sentence, known):
            if name.lower() not in entity_names:
                entity_names[name.lower()] = name

        subject = _pick_subject(sentence, known)
        inferred_subject = False
        if subject is None:
            # A shorthand mention ("pack temperature") beats carrying whatever
            # proper noun happened to appear last -- it is evidence from this
            # sentence, not from a neighbouring one.
            subject = _distinctive_reference(sentence, known)
        if subject:
            current_subject, carry = subject, 0
        elif current_subject and carry < CARRY_LIMIT:
            carry += 1
            subject = current_subject
        else:
            current_subject = None
            subject = default_subject
            inferred_subject = subject is not None
        confidence_penalty = 0.12 if inferred_subject else 0.0
        split = _split_on_comparator(sentence)
        value = _value_in(split[2]) if split else _value_in(sentence)

        is_requirement = bool(_REQUIREMENT_CUES.search(sentence))
        is_decision = bool(_DECISION_CUES.search(sentence))
        is_event = bool(_EVENT_CUES.search(sentence))
        is_risk = bool(_RISK_CUES.search(sentence))
        is_observation = bool(_OBSERVATION_CUES.search(sentence))
        is_assumption = bool(_ASSUMPTION_CUES.search(sentence))

        # --- constraint / requirement ---------------------------------
        if split and value and is_requirement and subject:
            left, op, _ = split
            predicate = (
                canonical_predicate(sentence)
                or _predicate_from_unit(value[1], value[0])
                or _fallback_predicate(left)
            )
            ex.constraints.append(
                ExtractedConstraint(
                    text=sentence, subject=subject, predicate=predicate,
                    operator=op, value=value[0], unit=value[1],
                    kind="Requirement" if re.search(r"\b(must|shall)\b", sentence, re.I)
                         else "Constraint",
                    confidence=0.72 - confidence_penalty,
                )
            )
            continue

        # --- decision -------------------------------------------------
        if is_decision:
            ex.decisions.append(
                ExtractedDecision(
                    text=sentence,
                    subject=subject or "",
                    rationale=_after_because(sentence),
                    confidence=0.62,
                )
            )
            continue

        # --- event ----------------------------------------------------
        if is_event:
            ex.events.append(
                ExtractedEvent(
                    text=sentence,
                    subject=subject or "",
                    affects=[e for e in find_entities(sentence, known)][:4],
                    confidence=0.65,
                )
            )
            # "reduced from 70C to 60C" also asserts a new value
            pair = re.search(
                r"(?:reduced|lowered|raised|increased|changed|revised)\s+from\s+"
                r"(?P<old>[^,;]+?)\s+to\s+(?P<new>[^,;.]+)", sentence, re.I
            )
            # An announcement is also an assertion: "X was withdrawn" says both
            # that something happened and that X is now withdrawn.
            _emit_polarity(ex, sentence, subject, is_observation, confidence_penalty)
            if pair and subject:
                newv = _value_in(pair.group("new"))
                if newv:
                    predicate = (
                        canonical_predicate(sentence)
                        or _predicate_from_unit(newv[1], newv[0])
                        or _fallback_predicate(sentence)
                    )
                    ex.claims.append(
                        ExtractedClaim(
                            text=sentence, subject=subject, predicate=predicate,
                            value=newv[0], unit=newv[1], confidence=0.68, kind="Claim",
                        )
                    )
            continue

        # --- risk -----------------------------------------------------
        if is_risk:
            ex.risks.append(
                ExtractedRisk(
                    text=sentence, subject=subject or "",
                    severity=0.6 if re.search(r"\bcritical|severe|major\b", sentence, re.I) else 0.45,
                    confidence=0.55,
                )
            )
            continue

        # --- claim / observation --------------------------------------
        if value and subject:
            predicate = (
                canonical_predicate(sentence)
                or _predicate_from_unit(value[1], value[0])
                or _fallback_predicate(
                    sentence[: sentence.find(value[0])]
                    if value[0] in sentence else sentence
                )
            )
            kind = ("Observation" if is_observation
                    else "Assumption" if is_assumption else "Claim")
            ex.claims.append(
                ExtractedClaim(
                    text=sentence, subject=subject, predicate=predicate,
                    value=value[0], unit=value[1],
                    confidence=(0.70 if is_observation else 0.65) - confidence_penalty,
                    kind=kind,
                )
            )
            continue

        # --- polarity claim ("X is now deprecated") --------------------
        if _emit_polarity(ex, sentence, subject, is_observation, confidence_penalty):
            continue

        # --- relationship cues ----------------------------------------
        rel = _relationship(sentence, known)
        if rel:
            ex.relationships.append(rel)

    ex.entities = [
        ExtractedEntity(name=name, kind=_guess_kind(name), description="")
        for name in entity_names.values()
    ]
    return ex


def _emit_polarity(
    ex: Extraction, sentence: str, subject: str | None,
    is_observation: bool, penalty: float,
) -> bool:
    """Record "X is now deprecated" as a claim. Returns whether one was added.

    Called from the event branch as well as standalone, because words like
    "deprecated" and "withdrawn" are event cues *and* state assertions. A
    regulation notice announcing a withdrawal is both an event and the claim
    that the thing is now withdrawn; emitting only the event loses the value
    that a later document could contradict.
    """
    if not subject:
        return False
    match = _POLARITY_RE.search(sentence)
    if not match:
        return False
    state = match.group("state").lower()
    predicate = _POLARITY_PREDICATE.get(state, "status")
    if match.group("neg"):
        # The predicate is a property of the word as written ("compliant" ->
        # compliance status), so it is resolved before the value is flipped.
        state = _POLARITY_OPPOSITE.get(state, state)
    ex.claims.append(
        ExtractedClaim(
            text=sentence, subject=subject,
            predicate=predicate,
            value=state, unit=None,
            confidence=(0.66 if is_observation else 0.62) - penalty,
            kind="Observation" if is_observation else "Claim",
        )
    )
    return True


def _after_because(sentence: str) -> str:
    m = re.search(r"\b(?:because|since|as|due to|owing to)\b(.+)", sentence, re.I)
    return m.group(1).strip(" .") if m else ""


_REL_PATTERNS: list[tuple[re.Pattern[str], EdgeType]] = [
    (re.compile(r"\bdepends? on\b|\brelies on\b|\bcontingent on\b", re.I), EdgeType.DEPENDS_ON),
    (re.compile(r"\brequires?\b|\bneeds?\b|\bprerequisite\b", re.I), EdgeType.REQUIRES),
    (re.compile(r"\bcauses?\b|\bleads? to\b|\bresults? in\b|\btriggers?\b", re.I), EdgeType.CAUSES),
    (re.compile(r"\baffects?\b|\bimpacts?\b|\binfluences?\b", re.I), EdgeType.AFFECTS),
    (re.compile(r"\bsupersedes?\b|\breplaces?\b", re.I), EdgeType.SUPERSEDES),
    (re.compile(r"\bimplements?\b|\bsatisfies\b|\brealis\w+\b", re.I), EdgeType.IMPLEMENTS),
    (re.compile(r"\bconstrains?\b|\blimits?\b|\bgoverns?\b", re.I), EdgeType.CONSTRAINS),
    (re.compile(r"\bcontradicts?\b|\bconflicts? with\b|\bdisagrees with\b", re.I),
     EdgeType.CONTRADICTS),
    (re.compile(r"\bsupports?\b|\bcorroborates?\b|\bconfirms?\b", re.I), EdgeType.SUPPORTS),
    (re.compile(r"\bowned by\b|\bmanaged by\b|\boperated by\b", re.I), EdgeType.OWNED_BY),
]


def _relationship(sentence: str, known: Iterable[str]) -> ExtractedRelationship | None:
    for pattern, etype in _REL_PATTERNS:
        m = pattern.search(sentence)
        if not m:
            continue
        left = find_entities(sentence[: m.start()], known)
        right = find_entities(sentence[m.end():], known)
        if left and right:
            return ExtractedRelationship(
                source=left[-1], target=right[0], type=etype,
                rationale=sentence, weight=0.7, confidence=0.6,
            )
    return None


def _guess_kind(name: str) -> str:
    low = name.lower()
    if re.search(r"\b(ltd|inc|gmbh|corp|corporation|supplier|vendor|partners?)\b", low):
        return "Organization"
    if re.search(r"\b(unece|iso|iec|en|astm|regulation|directive|standard)\b", low):
        return "Regulation"
    if re.search(r"\b(pack|module|battery|motor|sensor|controller|unit|station)\b", low):
        return "Component"
    if re.search(r"\b(fleet|programme|program|project|phase|deployment)\b", low):
        return "Project"
    if re.search(r"\b(architecture|system|platform|service|api)\b", low):
        return "System"
    return "Concept"
