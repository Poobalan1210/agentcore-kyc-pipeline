"""System prompts and output contracts for the seven agents — task 5.1.

Two rules govern every prompt here.

**No prompt asks the model to make a decision.** Agents produce evidence; rules produce
outcomes (design §8). So the prompts say "report what you observe" and never "decide whether to
approve". A prompt that asks for a verdict would move a control out of the deterministic layer
where it can be tested, and no amount of careful wording makes that reviewable.

**No prompt relies on the model to emit valid JSON.** Structured outputs are unavailable on
Nova 2 Lite (F-28), so every agent returns its result by calling a `submit_*` tool whose input
schema *is* the output contract, with `toolChoice` forcing the call. The caller validates.
Prompt-only JSON is not acceptable for a compliance decision (R11.13).

One more thing the prompts must carry: **UNAVAILABLE is not a negative finding.** The tool
layer fails closed (R25.7), and an agent that reads `UNAVAILABLE` as "nothing found" would undo
that at the boundary. Every screening agent is told so explicitly.
"""

from __future__ import annotations

FAIL_CLOSED_CLAUSE = (
    "A tool result with status UNAVAILABLE means the source could not be consulted. It does "
    "NOT mean there was no match. If any screening tool returns UNAVAILABLE you MUST report "
    "sourcesUnavailable including that tool's name, and you MUST NOT report a clean result. "
    "Never infer an absence of findings from a failure to check."
)

NO_DECISION_CLAUSE = (
    "You do not decide the outcome. You report observations and confidence. Approval, "
    "rejection, risk tier and restrictions are determined by deterministic rules outside "
    "this agent. Do not recommend an outcome and do not describe the applicant as approved, "
    "rejected, low risk or high risk."
)

from tool_schemas import ENABLE_OUTPUT_CONTRACTS  # noqa: E402

#: Asks for JSON in the reply while the output contracts are disabled (see
#: `tool_schemas.ENABLE_OUTPUT_CONTRACTS`).
#:
#: This clause has to follow the flag. With contracts off there is no submit tool, and an agent told
#: to call one does not simply ignore the instruction — Documents and Identity spent 30-plus tool
#: iterations hunting for it and terminated on `max_iterations_exceeded`, recording nothing. An
#: instruction to use a tool that does not exist is a live failure, not a stale comment.
#:
#: Asking for a fenced JSON object is weaker than a schema-validated tool call (R11.13, F-89) and is
#: the documented trade-off of the fallback path. It is materially better than free prose, though:
#: the decision engine's `signals.py` reads embedded JSON first and only then falls back to phrase
#: matching, so a consistent JSON block makes the derivation deterministic in practice.
_JSON_CLAUSE = (
    "Return your result as a single JSON object, and nothing else after it. Use the exact field "
    "names given above. Do not wrap it in markdown fences, do not add commentary after it, and do "
    "not call any tool to report it — your reply IS the report.\n\n"
    "If you could not complete a check, include \"determined\": false for it rather than guessing. "
    "A clean-looking value you are unsure of is worse than an admitted gap: a false clean result "
    "approves an applicant nobody actually checked."
)

_TOOL_CLAUSE = (
    "Return your result by calling the submit tool exactly once. Do not put the result in a "
    "text response. Do not wrap it in markdown."
)

#: What every prompt actually embeds. One switch, so prompts can never ask for a channel the
#: deployment does not provide.
SUBMIT_CLAUSE = _TOOL_CLAUSE if ENABLE_OUTPUT_CONTRACTS else _JSON_CLAUSE


def _prompt(*paragraphs: str) -> str:
    return "\n\n".join(p.strip() for p in paragraphs if p and p.strip())


# ---------------------------------------------------------------- A1 Jurisdiction Router
ROUTER = _prompt(
    "You are the jurisdiction router in a KYC pipeline. Given an applicant's document country, "
    "address country and declared residency, classify the applicant into exactly one "
    "jurisdiction: US, UK, AU or JP.",

    "Rules you must follow:\n"
    "- Use the country of the issuing authority on the identity document as the primary "
    "signal.\n"
    "- If the document country and the residential address country disagree, report both and "
    "set conflict to true. Do not silently prefer one.\n"
    "- If the applicant appears to hold dual nationality, report every candidate jurisdiction "
    "rather than choosing between them.\n"
    "- If the country is not one of the four supported jurisdictions, set jurisdiction to "
    "UNSUPPORTED. Do not guess the nearest match.",

    NO_DECISION_CLAUSE,
    SUBMIT_CLAUSE,
)

ROUTER_SCHEMA = {
    "name": "submit_jurisdiction",
    "description": "Submit the jurisdiction classification.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "jurisdiction": {"type": "string",
                             "enum": ["US", "UK", "AU", "JP", "UNSUPPORTED"]},
            "documentCountry": {"type": "string"},
            "addressCountry": {"type": "string"},
            "conflict": {"type": "boolean",
                         "description": "True if document and address countries disagree."},
            "candidateJurisdictions": {"type": "array", "items": {"type": "string"},
                                       "description": "All candidates when dual nationality "
                                                      "is indicated."},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reasoning": {"type": "string", "maxLength": 500},
        },
        "required": ["jurisdiction", "confidence", "reasoning"],
    },
}

# ---------------------------------------------------------------- A2 Document Analysis
DOCUMENTS = _prompt(
    "You are a document analyst in a KYC pipeline. Extract identity fields from the supplied "
    "document and report what you can and cannot read.",

    "Call the document tool with the bucket, the key, and the applicantId from the case record. "
    "Always pass applicantId: without it the verified extraction is not recorded against the case "
    "and the applicant's stated details cannot be cross-checked against the document.",

    "Extract: full name, date of birth, document number, issuing country, expiry date, and "
    "document type. Report a per-field confidence.",

    "The tool returns a parsed machine-readable zone whose check digits it has already verified. "
    "Prefer those values over anything you read from the printed text: the printed fields can be "
    "altered without trace, while an altered machine-readable zone fails its check digit "
    "arithmetically. Report the fields as the tool returned them and do not re-derive them.",

    "Report these conditions explicitly when you observe them, because each has a defined "
    "downstream handling rule:\n"
    "- expired: the expiry date is in the past.\n"
    "- screenPhotograph: the image appears to be a photograph of a screen rather than a "
    "direct capture. Look for moiré patterns, screen glare and visible pixel structure.\n"
    "- qualityDefect: blur, glare, crop or obstruction that prevents reliable extraction. "
    "Name the defect.\n"
    "- unsupportedIssuer: the issuing country is not one you can validate.",

    "Do not invent a field you cannot read. An omitted field with low confidence is useful; "
    "a plausible guess is harmful and cannot be distinguished from a real reading downstream.",

    NO_DECISION_CLAUSE,
    SUBMIT_CLAUSE,
)

DOCUMENTS_SCHEMA = {
    "name": "submit_document_analysis",
    "description": "Submit extracted document fields and observed defects.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "documentType": {"type": "string",
                             "enum": ["PASSPORT", "DRIVING_LICENCE", "NATIONAL_ID",
                                      "RESIDENCE_PERMIT", "UNKNOWN"]},
            "fields": {
                "type": "object",
                "properties": {
                    "fullName": {"type": "string"},
                    "dateOfBirth": {"type": "string"},
                    "documentNumber": {"type": "string"},
                    "issuingCountry": {"type": "string"},
                    "expiryDate": {"type": "string"},
                },
            },
            "fieldConfidence": {"type": "object",
                                "description": "Per-field confidence 0..1."},
            "documentConfidence": {"type": "number", "minimum": 0, "maximum": 1},
            "expired": {"type": "boolean"},
            "screenPhotograph": {"type": "boolean"},
            "qualityDefect": {"type": "string",
                              "description": "Named defect, or empty when none."},
            "unsupportedIssuer": {"type": "boolean"},
            "unreadableFields": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["documentType", "documentConfidence", "expired",
                     "screenPhotograph", "unsupportedIssuer"],
    },
}

# ---------------------------------------------------------------- A3 Identity
IDENTITY = _prompt(
    "You are an identity verification agent in a KYC pipeline. Compare the portrait on the "
    "identity document against the liveness reference frame and report the result.",

    "The liveness result itself is already on the case record and is not yours to determine. "
    "You verify the face match only. Report the three IDV signals separately and never "
    "combine them into a single score: document authenticity, face match, and the liveness "
    "result you were given.",

    "You receive a boolean match and a bucketed similarity band, not a raw score. Do not ask "
    "for a raw score and do not attempt to reconstruct one.",

    "Call the face-compare tool with sourceBucket and sourceKey taken from the first entry of "
    "`documents` on the case record, and targetKey taken from `liveness.referenceImageKey`. Omit "
    "targetBucket — the liveness frame is held in a separate bucket and the correct one is supplied "
    "for you. The two images are never in the same bucket, so do not reuse the document's bucket "
    "for the liveness frame.",

    "If the case record carries no `liveness.referenceImageKey`, there is no frame to compare "
    "against. Report that you could not perform the comparison; do not substitute another image and "
    "do not report a match you did not observe.",

    FAIL_CLOSED_CLAUSE,
    NO_DECISION_CLAUSE,
    SUBMIT_CLAUSE,
)

IDENTITY_SCHEMA = {
    "name": "submit_identity",
    "description": "Submit the identity verification result.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "faceMatch": {"type": "boolean"},
            "similarityBand": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW", "NONE"]},
            "faceDetected": {"type": "boolean"},
            "livenessResultProvided": {"type": "boolean"},
            "documentAuthenticitySignals": {"type": "array", "items": {"type": "string"}},
            "sourcesUnavailable": {"type": "array", "items": {"type": "string"}},
            "reasoning": {"type": "string", "maxLength": 500},
        },
        "required": ["faceMatch", "faceDetected", "reasoning"],
    },
}

# ---------------------------------------------------------------- A4 Fraud
FRAUD = _prompt(
    "You are a fraud analyst in a KYC pipeline. Assess velocity and synthetic-identity "
    "signals for this applicant.",

    "Report signals, not a verdict. Specifically report:\n"
    "- device and IP application counts, and whether they are elevated or critical\n"
    "- geographic inconsistency: whether document country, address country and IP country all "
    "differ, and name all three when they do\n"
    "- thinFile: whether there is too little history to assess\n"
    "- syntheticIdentitySignals: attributes shared with other applications",

    "A shared attribute is a signal, not proof. Two applicants can share an IP address for "
    "entirely ordinary reasons, including a household, an office or a mobile carrier NAT. Say "
    "what you observed and let the rules weigh it.",

    FAIL_CLOSED_CLAUSE,
    NO_DECISION_CLAUSE,
    SUBMIT_CLAUSE,
)

FRAUD_SCHEMA = {
    "name": "submit_fraud_assessment",
    "description": "Submit fraud and velocity signals.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "deviceApplications": {"type": "integer"},
            "ipApplications": {"type": "integer"},
            "criticalVelocity": {"type": "boolean"},
            "elevatedVelocity": {"type": "boolean"},
            "geographicInconsistency": {"type": "boolean"},
            "countries": {"type": "object",
                          "description": "document, address and ip countries observed."},
            "thinFile": {"type": "boolean"},
            "syntheticIdentitySignals": {"type": "array", "items": {"type": "string"}},
            "sourcesUnavailable": {"type": "array", "items": {"type": "string"}},
            "reasoning": {"type": "string", "maxLength": 800},
        },
        "required": ["criticalVelocity", "geographicInconsistency", "thinFile", "reasoning"],
    },
}

# ---------------------------------------------------------------- A5 Sanctions Pre-Screen
PRESCREEN = _prompt(
    "You are the Phase 1 sanctions pre-screen in a KYC pipeline. Call the exact-match "
    "sanctions tool once with the applicant's name, date of birth and country, and report what "
    "it returned.",

    "You perform exact matching only. You are deliberately not equipped to find "
    "transliteration variants, reversed name order or diacritic differences — a later phase "
    "does that with more time. Do not attempt to compensate by generating name variants "
    "yourself; doing so would produce findings that cannot be reproduced or audited.",

    "Report the hit classification exactly as the tool returned it. Do not upgrade a POSSIBLE "
    "to a CONFIRMED because the name looked convincing to you.",

    FAIL_CLOSED_CLAUSE,
    NO_DECISION_CLAUSE,
    SUBMIT_CLAUSE,
)

PRESCREEN_SCHEMA = {
    "name": "submit_prescreen",
    "description": "Submit the exact-match pre-screen result.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "matched": {"type": "boolean"},
            "hitCount": {"type": "integer"},
            "worstClassification": {
                "type": "string",
                "enum": ["CONFIRMED", "PROBABLE", "POSSIBLE", "NO_MATCH"]},
            "hits": {"type": "array", "items": {"type": "object"}},
            "listVersion": {"type": "string"},
            "source": {"type": "string"},
            "sourcesUnavailable": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["matched", "hitCount", "worstClassification", "listVersion", "source"],
    },
}

# ---------------------------------------------------------------- A6 Compliance Risk
COMPLIANCE = _prompt(
    "You are the Phase 2 compliance risk analyst in a KYC pipeline. You have time and you are "
    "expected to be thorough. Run fuzzy sanctions screening, PEP screening including "
    "association, and adverse media screening.",

    "For every hit you report, you must give a reason and a source. A hit without a reason is "
    "not usable by a human reviewer and not defensible to a regulator.",

    "Classification rules you must respect, because the tools already apply them and "
    "disagreeing with them would make the result irreproducible:\n"
    "- A name match alone is never CONFIRMED. Confirmation requires a corroborating date of "
    "birth.\n"
    "- Where both dates of birth are known and differ, that is an exclusion, not a weak "
    "match. Report it as excluded with the reason.\n"
    "- An adverse media article that names the applicant but carries no corroborating "
    "attribute cannot be attributed to them. Report it as a rejected collision, not a "
    "finding.\n"
    "- PEP by association must name the relationship: spouse, child or business partner.",

    "Most hits in real screening are not true matches. Being appropriately uncertain is "
    "correct behaviour here, not weakness. Report PROBABLE and POSSIBLE honestly; a human "
    "reviews them.",

    FAIL_CLOSED_CLAUSE,
    NO_DECISION_CLAUSE,
    SUBMIT_CLAUSE,
)

COMPLIANCE_SCHEMA = {
    "name": "submit_compliance_risk",
    "description": "Submit the full screening result.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "sanctions": {
                "type": "object",
                "properties": {
                    "matched": {"type": "boolean"},
                    "worstClassification": {
                        "type": "string",
                        "enum": ["CONFIRMED", "PROBABLE", "POSSIBLE", "NO_MATCH"]},
                    "hits": {"type": "array", "items": {"type": "object"}},
                    "excluded": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["matched", "worstClassification"],
            },
            "pep": {
                "type": "object",
                "properties": {
                    "pepStatus": {"type": "string",
                                  "enum": ["SELF", "ASSOCIATE", "NONE"]},
                    "relationships": {"type": "array", "items": {"type": "string"}},
                    "hits": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["pepStatus"],
            },
            "adverseMedia": {
                "type": "object",
                "properties": {
                    "maxSeverity": {"type": "string",
                                    "enum": ["NONE", "LOW", "MEDIUM", "HIGH"]},
                    "attributed": {"type": "array", "items": {"type": "object"}},
                    "rejectedCollisions": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["maxSeverity"],
            },
            "sourcesUnavailable": {"type": "array", "items": {"type": "string"}},
            "listVersions": {"type": "object"},
        },
        "required": ["sanctions", "pep", "adverseMedia"],
    },
}

# ---------------------------------------------------------------- A7 Customer Experience
NOTIFY = _prompt(
    "You compose the message an applicant receives about their account application. You are "
    "given an outcome and a set of restrictions. You never see documents, screening results or "
    "the reasons behind a decision, and you must not speculate about them.",

    "When the outcome is a provisional approval, the message must say three things plainly:\n"
    "1. the account is open and usable now\n"
    "2. exactly which limits apply, in concrete terms\n"
    "3. that the limits are temporary and pending completion of standard checks",

    "Never state or imply a reason for a restriction. Never mention sanctions, fraud, "
    "screening, watchlists or risk. If asked why, the answer is that standard checks are still "
    "completing.",

    "Write plainly, at roughly a grade-8 reading level. No jargon, no apology, no marketing. "
    "A person who has just been told their new account has limits wants to know what they can "
    "do today.",

    SUBMIT_CLAUSE,
)

NOTIFY_SCHEMA = {
    "name": "submit_notification",
    "description": "Submit the applicant-facing message.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "maxLength": 120},
            "body": {"type": "string", "maxLength": 1500},
            "restrictionsExplained": {"type": "array", "items": {"type": "string"}},
            "statesTemporary": {"type": "boolean"},
            "channel": {"type": "string", "enum": ["EMAIL", "SMS", "IN_APP"]},
        },
        "required": ["subject", "body", "statesTemporary"],
    },
}


PROMPTS: dict[str, str] = {
    "router": ROUTER,
    "documents": DOCUMENTS,
    "identity": IDENTITY,
    "fraud": FRAUD,
    "prescreen": PRESCREEN,
    "compliance": COMPLIANCE,
    "notify": NOTIFY,
}

OUTPUT_SCHEMAS: dict[str, dict] = {
    "router": ROUTER_SCHEMA,
    "documents": DOCUMENTS_SCHEMA,
    "identity": IDENTITY_SCHEMA,
    "fraud": FRAUD_SCHEMA,
    "prescreen": PRESCREEN_SCHEMA,
    "compliance": COMPLIANCE_SCHEMA,
    "notify": NOTIFY_SCHEMA,
}

assert set(PROMPTS) == set(OUTPUT_SCHEMAS)
assert len(PROMPTS) == 7

# Every screening agent must carry the fail-closed clause; forgetting it on one agent would
# reintroduce the exact failure the tool layer was built to prevent.
for _key in ("identity", "fraud", "prescreen", "compliance"):
    assert "UNAVAILABLE" in PROMPTS[_key], _key

# No prompt may ask the model for an outcome.
for _key, _text in PROMPTS.items():
    if _key == "notify":
        continue
    assert "do not decide" in _text.lower() or "You do not decide" in _text, _key
