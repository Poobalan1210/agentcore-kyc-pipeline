"""MCP tool schemas for the Gateway targets.

Each of the seven tools is registered on each of the four Gateways, so Cedar can authorise them
individually (R14.2) even though they share one Lambda.

Two things here are load-bearing:

  * **Descriptions are part of the security surface.** The agent chooses tools by description,
    so a vague description produces the wrong tool call. R10.3 also makes any description change
    a release-gated event, because it changes behaviour as surely as a prompt change does.
  * **The Cedar action name is `TargetName___tool_name` — three underscores** (F-35). The target
    names below are what the Phase 8 policy files must reference; getting the separator wrong
    means every policy silently fails to match, which is indistinguishable from having no
    policy at all.
"""

from __future__ import annotations

from typing import Any

#: Gateway target name per tool. The Cedar action is f"{TARGET_NAMES[tool]}___{tool}".
TARGET_NAMES: dict[str, str] = {
    "s3_get_document": "DocumentStore",
    "rekognition_compare_faces": "FaceCompare",
    "velocity_check": "VelocityStore",
    "sanctions_exact_match": "SanctionsExact",
    "sanctions_fuzzy": "SanctionsFuzzy",
    "pep_lookup": "PepList",
    "adverse_media": "AdverseMedia",
    # Output contracts (R11.13, F-89). Each agent's only way to report a finding. They exist
    # because prose is not a data interface: across two live runs with an identical prompt and
    # fixture, one agent returned `"confidence": 0.96` as JSON and the next expressed the same
    # answer in a sentence, so the derived signals differed between runs and R3.5's "identical
    # outcome on every evaluation" could not hold. A tool call with a JSON Schema is validated by
    # the service, so the shape is not the model's choice.
    "submit_document_analysis": "SubmitDocument",
    "submit_prescreen": "SubmitPreScreen",
    "submit_fraud_assessment": "SubmitFraud",
    "submit_jurisdiction": "SubmitJurisdiction",
    "submit_identity": "SubmitIdentity",
    "submit_compliance_risk": "SubmitComplianceRisk",
    "submit_notification": "SubmitNotification",
}

#: `Agent.tools` in the CDK config names tools by short alias while this registry uses the real
#: tool names. The two drifted because nothing forced them to agree — the harnesses were granted
#: the `@kyc-gw/*` wildcard, so the alias list was documentation rather than configuration.
#:
#: Now that AllowedTools is scoped per agent, the mapping is load-bearing: an unmapped alias means
#: an agent silently loses a tool it needs. `agent_tool_names` therefore raises on an unknown alias
#: instead of skipping it.
AGENT_TOOL_ALIASES: dict[str, str] = {
    "doc_extract": "s3_get_document",
    "face_compare": "rekognition_compare_faces",
    "velocity_check": "velocity_check",
    "sanctions_exact": "sanctions_exact_match",
    "sanctions_fuzzy": "sanctions_fuzzy",
    "pep_lookup": "pep_lookup",
    "adverse_media": "adverse_media",
}


def agent_tool_names(aliases: tuple[str, ...] | list[str]) -> list[str]:
    """Real tool names for an agent's declared aliases. Raises on an unknown alias.

    Raising rather than skipping is deliberate. A silently dropped tool produces an agent that
    reports it cannot complete its checks, which reads as a degraded case rather than as the
    configuration error it is.
    """
    resolved = []
    for alias in aliases:
        real = AGENT_TOOL_ALIASES.get(alias)
        if real is None:
            raise KeyError(
                "unknown agent tool alias %r — add it to AGENT_TOOL_ALIASES" % (alias,))
        resolved.append(real)
    return resolved


#: The submit_* tools, by the agent that must call exactly one of them.
#: Output contracts are BUILT but not ENABLED at runtime. Set to True to turn them on.
#:
#: R11.13 requires structured output to travel as tool-use, and F-89 measured why: an agent's prose
#: changed shape between two otherwise identical runs. The contracts here are the correct answer and
#: they are fully implemented and tested.
#:
#: They are switched off because enabling them took the live pipeline from four of four Phase 1
#: agents succeeding to zero of five, across four distinct failures — malformed tool sequences on
#: two agents, an iteration loop on a third, and a model-access denial on a fourth. For a proof of
#: concept with a fixed date, a working pipeline whose weakness is documented beats a more correct
#: one that does not run. The fallback path derives signals from prose and marks anything ambiguous
#: as UNAVAILABLE, so its failure mode is a conservative routing to a human.
#:
#: **This is a deliberate, dated trade-off, not an oversight.** Turning it on is a one-line change
#: plus the four investigations above.
ENABLE_OUTPUT_CONTRACTS = False

#: All seven, one per agent — the set `OutputContractNames` always listed.
#:
#: Only three existed at first, and every prompt already ended with "return your result by calling
#: the submit tool exactly once". So Router, Identity and Notify were instructed to call a tool that
#: did not exist; the model emitted a tool-use block anyway and the runtime rejected it with "Model
#: produced invalid sequence as part of ToolUse". A prompt that references a tool the agent does not
#: have is not a soft mismatch — it is a hard failure.
ALL_OUTPUT_CONTRACTS: dict[str, str] = {
    "router": "submit_jurisdiction",
    "documents": "submit_document_analysis",
    "identity": "submit_identity",
    "fraud": "submit_fraud_assessment",
    "prescreen": "submit_prescreen",
    "compliance": "submit_compliance_risk",
    "notify": "submit_notification",
}

#: What the deployed system actually uses. Empty while the contracts are disabled, so every consumer
#: — prompts, AllowedTools, model selection, Gateway targets — reverts together rather than each
#: needing its own flag check.
OUTPUT_CONTRACTS: dict[str, str] = dict(ALL_OUTPUT_CONTRACTS) if ENABLE_OUTPUT_CONTRACTS else {}


def cedar_action(tool: str) -> str:
    """Three underscores. F-35."""
    return "%s___%s" % (TARGET_NAMES[tool], tool)


_NAME = {"type": "string",
         "description": "Full legal name exactly as it appears on the document."}
_DOB = {"type": "string",
        "description": "Date of birth, ISO 8601 YYYY-MM-DD. Omit only if genuinely unknown; "
                       "screening cannot corroborate a match without it."}
_COUNTRY = {"type": "string",
            "description": "ISO 3166-1 alpha-2 country code."}

SCHEMAS: dict[str, dict[str, Any]] = {
    "s3_get_document": {
        "name": "s3_get_document",
        "description": (
            "Read an uploaded identity document from the jurisdiction evidence bucket and extract "
            "its fields. Returns size, content type, document type, the text read off the "
            "document, and the parsed machine-readable zone with its check digits verified "
            "(surname, given names, date of birth, document number, nationality, expiry). Does "
            "NOT return the document bytes."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "bucket": {"type": "string",
                           "description": "Jurisdiction evidence bucket name."},
                "key": {"type": "string", "description": "Object key."},
                "documentType": {"type": "string",
                                 "description": "PASSPORT, DRIVING_LICENCE, NATIONAL_ID."},
                # Optional, and not in `required`: without it the extraction is still returned, it
                # just is not recorded on the case for the decision engine to cross-check against
                # the applicant's declared details.
                "applicantId": {"type": "string",
                                "description": (
                                    "Applicant id, so the verified extraction is recorded on the "
                                    "case record for the declared-details cross-check.")},
            },
            "required": ["bucket", "key"],
        },
    },
    "rekognition_compare_faces": {
        "name": "rekognition_compare_faces",
        "description": (
            "Compare the portrait on an identity document against the liveness reference "
            "frame. Returns a boolean match and a bucketed similarity band (HIGH/MEDIUM/LOW). "
            "The raw similarity score is deliberately not returned — the threshold is applied "
            "server side."),
        "inputSchema": {
            "type": "object",
            "properties": {
                # A bucket per image: the document is in the jurisdiction evidence bucket, while the
                # liveness frame is in the biometric bucket, which has no Object Lock so a real face
                # stays deletable within 24 hours (R36). They are never in the same bucket.
                "sourceBucket": {"type": "string",
                                 "description": (
                                     "Bucket holding the document. Use the jurisdiction evidence "
                                     "bucket from the case record.")},
                "sourceKey": {"type": "string",
                              "description": "Document object key, from the case record."},
                "targetBucket": {"type": "string",
                                 "description": (
                                     "Bucket holding the liveness reference frame. Omit it and the "
                                     "correct bucket is used automatically.")},
                "targetKey": {"type": "string",
                              "description": (
                                  "Liveness reference frame key — `liveness.referenceImageKey` on "
                                  "the case record.")},
                "threshold": {"type": "number",
                              "description": "Similarity threshold, default 90."},
            },
            "required": ["sourceBucket", "sourceKey", "targetKey"],
        },
    },
    # --- output contracts (R11.13) -------------------------------------------------------
    #
    # Every field the decision engine needs is `required`, so a partial finding is rejected by
    # schema validation rather than arriving as a silent gap. `confidence` is a number here, not
    # a string: a model asked for a number in a tool call supplies one, whereas the same value in
    # prose arrived variously as `0.96`, `96`, and "high confidence" across runs.
    "submit_document_analysis": {
        "name": "submit_document_analysis",
        "description": (
            "Report the result of document analysis. This is the ONLY way to record a document "
            "finding — text in your reply is not read by the decision engine. Call this exactly "
            "once, last, after every other check you intend to make."),
        "inputSchema": {
            "type": "object",
            "properties": {
                # No `minimum`/`maximum`: the Gateway's inline MCP schema rejects JSON Schema
                # numeric constraints ("0 subschemas matched instead of one"), which cost 12 failed
                # targets. The bound is enforced in the tool instead — see `_submit` — so the
                # guarantee survives even though it moved from declaration to code.
                "confidence": {
                    "type": "number",
                    "description": "Overall extraction confidence, 0.0 to 1.0 inclusive."},
                "documentType": {"type": "string",
                                 "description": "PASSPORT, DRIVING_LICENCE, NATIONAL_ID."},
                "issuingCountry": _COUNTRY,
                "expired": {"type": "boolean",
                            "description": "True if the expiry date has passed (R28.1)."},
                "screenPhotograph": {
                    "type": "boolean",
                    "description": "True if this appears to be a photograph of a display "
                                   "rather than the document itself (R28.3)."},
                "qualityDefects": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Named defects, e.g. blurred, glare, cropped. R28.4 requires "
                                   "the defect be named so a step-up can be acted on."},
                "unsupportedIssuer": {
                    "type": "boolean",
                    "description": "True if the issuing country is outside the supported set "
                                   "(R28.2). This routes to review, never to rejection."},
            },
            "required": ["confidence", "expired", "screenPhotograph", "unsupportedIssuer"],
        },
    },
    "submit_prescreen": {
        "name": "submit_prescreen",
        "description": (
            "Report the exact-match sanctions pre-screen result. This is the ONLY way to record "
            "a screening finding. Call this exactly once, last. If you could not complete the "
            "screening, set determined=false rather than guessing matched."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "determined": {
                    "type": "boolean",
                    "description": "False if screening could not be completed. When false the "
                                   "case goes to a human — which is correct. Never report "
                                   "matched=false to mean 'I could not check'."},
                "matched": {"type": "boolean",
                            "description": "True if any list entry matched."},
                # `number`, not `integer`: the Gateway inline schema accepts only the base JSON
                # types the existing seven tools use. Non-negativity is checked in the tool.
                "hitCount": {"type": "number",
                             "description": "Count of list entries matched. 0 when clean."},
                "strongestMatch": {
                    "type": "string",
                    "description": "CONFIRMED, PROBABLE, POSSIBLE or NO_MATCH. A name alone "
                                   "never confirms (R27.4)."},
                "listsConsulted": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["determined", "matched", "hitCount"],
        },
    },
    "submit_fraud_assessment": {
        "name": "submit_fraud_assessment",
        "description": (
            "Report device, velocity and synthetic-identity signals. This is the ONLY way to "
            "record a fraud finding. Call this exactly once, last. Report signals, not a "
            "verdict — whether they amount to fraud is a decision-engine rule."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "determined": {"type": "boolean",
                               "description": "False if the checks could not be completed."},
                "velocityCritical": {
                    "type": "boolean",
                    "description": "True only if a velocity threshold was breached. A clean "
                                   "result is false, not omitted."},
                "velocityDetail": {"type": "array", "items": {"type": "string"}},
                "sharedAttributeCount": {
                    "type": "number",
                    "description": "Identity attributes shared with other cases (R29.6)."},
                "thinFile": {"type": "boolean",
                             "description": "True if bureau history is insufficient (R29.4)."},
            },
            "required": ["determined", "velocityCritical"],
        },
    },
    "submit_jurisdiction": {
        "name": "submit_jurisdiction",
        "description": (
            "Report the jurisdiction classification. This is the ONLY way to record it. Call this "
            "exactly once, last. Report every candidate when they conflict — do not choose."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "determined": {"type": "boolean",
                               "description": "False if the jurisdiction could not be resolved."},
                "jurisdiction": {
                    "type": "string",
                    "description": "us, uk, au, jp, UNRESOLVED or UNSUPPORTED. UNSUPPORTED and "
                                   "UNRESOLVED both route to a human (R12.4) — never guess."},
                "candidates": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Every candidate jurisdiction when signals conflict, e.g. dual "
                                   "nationality (R12.5). Record all, choose none."},
                "documentCountry": _COUNTRY,
                "addressCountry": _COUNTRY,
                "ipCountry": _COUNTRY,
            },
            "required": ["determined", "jurisdiction"],
        },
    },
    "submit_identity": {
        "name": "submit_identity",
        "description": (
            "Report the identity verification result. This is the ONLY way to record it. Call "
            "this exactly once, last."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "determined": {
                    "type": "boolean",
                    "description": "False if the comparison could not be completed — an "
                                   "unreadable image is not a failed match."},
                "faceMatch": {"type": "boolean",
                              "description": "True if the selfie matches the document portrait."},
                "confidence": {"type": "number", "description": "0.0 to 1.0 inclusive."},
                "thinFile": {"type": "boolean",
                             "description": "True if bureau history is insufficient (R29.4)."},
                "deceasedIndicator": {
                    "type": "boolean",
                    "description": "True only on a positive deceased-person match (R29.5). This "
                                   "is the one signal that leads to rejection, so report it only "
                                   "when the source asserts it."},
            },
            "required": ["determined", "faceMatch"],
        },
    },
    "submit_compliance_risk": {
        "name": "submit_compliance_risk",
        "description": (
            "Report the full screening result. This is the ONLY way to record it. Call this "
            "exactly once, last. Report findings, not a verdict — the tier is a rule."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "determined": {"type": "boolean"},
                "pepStatus": {"type": "string",
                              "description": "SELF, ASSOCIATE or NONE (R4.5)."},
                "adverseMediaSeverity": {
                    "type": "string",
                    "description": "NONE, LOW, MEDIUM, HIGH or CRITICAL."},
                "strongestMatch": {
                    "type": "string",
                    "description": "CONFIRMED, PROBABLE, POSSIBLE or NO_MATCH. A name alone never "
                                   "confirms (R27.4)."},
                "hitCount": {"type": "number"},
                "listsConsulted": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["determined", "pepStatus", "adverseMediaSeverity"],
        },
    },
    "submit_notification": {
        "name": "submit_notification",
        "description": (
            "Report the applicant-facing message you produced. This is the ONLY way to record "
            "it. Call this exactly once, last."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "determined": {"type": "boolean"},
                "message": {"type": "string",
                            "description": "The plain-language message for the applicant."},
                "restrictionsExplained": {
                    "type": "boolean",
                    "description": "True if the message states which restrictions apply and why."},
            },
            "required": ["determined", "message"],
        },
    },
    "velocity_check": {
        "name": "velocity_check",
        "description": (
            "Count prior applications from the same device or IP address. Returns counts and "
            "boolean signals. Returns signals only — it does not decide whether the applicant "
            "is fraudulent; that is a decision-engine rule."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "deviceId": {"type": "string"},
                "ipAddress": {"type": "string"},
            },
        },
    },
    "sanctions_exact_match": {
        "name": "sanctions_exact_match",
        "description": (
            "Phase 1 pre-screen. Exact normalised name match against the consolidated "
            "sanctions list. Fast and deliberately narrow: it will NOT find transliteration "
            "or name-order variants — use sanctions_fuzzy for those. If the list cannot be "
            "reached it returns UNAVAILABLE, which must never be read as 'no match'."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": _NAME, "dob": _DOB, "country": _COUNTRY},
            "required": ["name"],
        },
    },
    "sanctions_fuzzy": {
        "name": "sanctions_fuzzy",
        "description": (
            "Phase 2 screening. Matches across transliteration (Mohammed/Muhammad), "
            "diacritics (José/Jose), reversed name order, and married/maiden names. Each hit "
            "is classified CONFIRMED, PROBABLE or POSSIBLE with a reason. A name match alone "
            "is never CONFIRMED — confirmation requires a corroborating date of birth."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _NAME, "dob": _DOB, "country": _COUNTRY,
                "alternateNames": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Other recorded names, e.g. maiden name."},
            },
            "required": ["name"],
        },
    },
    "pep_lookup": {
        "name": "pep_lookup",
        "description": (
            "Politically exposed person check, including exposure by association. Returns "
            "pepStatus SELF, ASSOCIATE or NONE, and for associates the relationship type "
            "(SPOUSE, CHILD, BUSINESS_PARTNER)."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": _NAME, "dob": _DOB, "country": _COUNTRY},
            "required": ["name"],
        },
    },
    "adverse_media": {
        "name": "adverse_media",
        "description": (
            "Search adverse media and attempt entity disambiguation. Articles are only "
            "attributed to the applicant when an attribute beyond the name agrees. Returns "
            "attributed articles, rejected name collisions with reasons, and a maximum "
            "severity. A shared name is not evidence."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": _NAME, "dob": _DOB, "country": _COUNTRY},
            "required": ["name"],
        },
    },
}

assert set(SCHEMAS) == set(TARGET_NAMES), set(SCHEMAS) ^ set(TARGET_NAMES)
# Seven evidence tools plus three output contracts (R11.13). The seven are the fixed set the talk
# describes; the three are a different kind of tool — they record a finding rather than fetch a
# fact — so they are counted separately rather than folded into the headline number.
# Contract tools are removed from the registries when disabled, so no Gateway target is created for
# them and no agent can see them. Filtering here rather than at each consumer keeps one switch.
#: Everything that is *built*, whether or not it is deployed. The contract tests assert against
#: these so the implementation stays verified while the flag is off — otherwise turning it back on
#: would mean re-earning confidence in code that had quietly rotted.
ALL_SCHEMAS: dict[str, Any] = dict(SCHEMAS)
ALL_TARGET_NAMES: dict[str, str] = dict(TARGET_NAMES)

if not ENABLE_OUTPUT_CONTRACTS:
    _contract_names = set(ALL_OUTPUT_CONTRACTS.values())
    SCHEMAS = {k: v for k, v in SCHEMAS.items() if k not in _contract_names}
    TARGET_NAMES = {k: v for k, v in TARGET_NAMES.items() if k not in _contract_names}

#: The seven evidence tools, always. Plus seven contracts when enabled.
_EXPECTED_TOOLS = 14 if ENABLE_OUTPUT_CONTRACTS else 7
assert len(SCHEMAS) == _EXPECTED_TOOLS, (len(SCHEMAS), _EXPECTED_TOOLS)
assert len(TARGET_NAMES) == _EXPECTED_TOOLS
assert len(ALL_OUTPUT_CONTRACTS) == 7
assert len(OUTPUT_CONTRACTS) == (7 if ENABLE_OUTPUT_CONTRACTS else 0)
assert set(OUTPUT_CONTRACTS.values()) <= set(SCHEMAS)
assert len([t for t in SCHEMAS if t not in set(OUTPUT_CONTRACTS.values())]) == 7


def inline_payload(tool: str) -> list[dict[str, Any]]:
    """The `toolSchema.inlinePayload` value for a Lambda gateway target."""
    return [SCHEMAS[tool]]


if __name__ == "__main__":
    import json
    for t in sorted(SCHEMAS):
        print("%-28s -> Cedar action %s" % (t, cedar_action(t)))
    print()
    print(json.dumps(SCHEMAS["sanctions_fuzzy"], indent=2))
