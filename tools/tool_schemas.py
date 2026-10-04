"""MCP tool schemas for the Gateway targets.

Each of the six evidence tools is registered on each of the four Gateways, so Cedar can authorise
them individually (R14.2) even though they share one Lambda.

Three things here are load-bearing:

  * **Descriptions are part of the security surface.** The agent chooses tools by description,
    so a vague description produces the wrong tool call. R10.3 also makes any description change
    a release-gated event, because it changes behaviour as surely as a prompt change does.
  * **The Cedar action name is `TargetName___tool_name` — three underscores** (F-35). The target
    names below are what the Phase 8 policy files must reference; getting the separator wrong
    means every policy silently fails to match, which is indistinguishable from having no
    policy at all.
  * **Every evidence tool takes an `applicantId` and reads everything else from the case record.**
    The schemas no longer offer the agent a way to choose what is checked: no name to screen, no
    document key to read, no threshold to apply. Where a schema still accepts a `name`, it is the
    name as the agent read it, recorded beside the result for comparison — and it is what the
    Gateway's prompt-attack guardrail inspects.
"""

from __future__ import annotations

from typing import Any

#: Gateway target name per tool. The Cedar action is f"{TARGET_NAMES[tool]}___{tool}".
TARGET_NAMES: dict[str, str] = {
    "s3_get_document": "DocumentStore",
    "rekognition_compare_faces": "FaceCompare",
    "velocity_check": "VelocityStore",
    "sanctions_screen": "SanctionsScreen",
    "pep_lookup": "PepList",
    "adverse_media": "AdverseMedia",
    # Output contracts (R11.13, F-89). Built and tested, deployed only when
    # ENABLE_OUTPUT_CONTRACTS is on. With the evidence tools recording facts themselves, a contract
    # records only the agent's narrative.
    "submit_document_analysis": "SubmitDocument",
    "submit_prescreen": "SubmitPreScreen",
    "submit_fraud_assessment": "SubmitFraud",
    "submit_briefing": "SubmitBriefing",
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
    "sanctions_screen": "sanctions_screen",
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
#: changed shape between two otherwise identical runs. The contracts here are fully implemented and
#: tested.
#:
#: They are switched off because enabling them took the live pipeline from four of four Phase 1
#: agents succeeding to zero of five, across four distinct failures — malformed tool sequences on
#: two agents, an iteration loop on a third, and a model-access denial on a fourth.
#:
#: That trade-off is now much cheaper than it was. The evidence tools record their own results on
#: the case record, and the decision engine reads those, so no outcome depends on what an agent
#: reports. With contracts off, an agent's reply is a narrative for the reviewer and nothing more.
ENABLE_OUTPUT_CONTRACTS = False

#: All seven, one per agent — the set `OutputContractNames` always listed.
#:
#: Only three existed at first, and every prompt already ended with "return your result by calling
#: the submit tool exactly once". So agents were instructed to call a tool that did not exist; the
#: model emitted a tool-use block anyway and the runtime rejected it with "Model produced invalid
#: sequence as part of ToolUse". A prompt that references a tool the agent does not have is not a
#: soft mismatch — it is a hard failure.
ALL_OUTPUT_CONTRACTS: dict[str, str] = {
    "briefing": "submit_briefing",
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


_APPLICANT = {"type": "string",
              "description": "The applicantId from the case record. The tool reads everything "
                             "else it needs from that record."}
_AGENT_NAME = {"type": "string",
               "description": "The applicant's full name exactly as it appears in the case "
                              "record's declaredIdentity. Recorded beside the result for "
                              "comparison; the tool screens the name on the record itself."}

SCHEMAS: dict[str, dict[str, Any]] = {
    "s3_get_document": {
        "name": "s3_get_document",
        "description": (
            "Read the identity document listed on the applicant's case record and extract its "
            "fields. Returns the text read off the document and the parsed machine-readable zone "
            "with its check digits verified (surname, given names, date of birth, document "
            "number, nationality, expiry), and records that extraction on the case record. Does "
            "NOT return the document bytes. Only documents already listed on the case can be "
            "read."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "applicantId": _APPLICANT,
                "bucket": {"type": "string",
                           "description": "Optional. The bucket of a document listed on the "
                                          "case. Omit to read the first listed document."},
                "key": {"type": "string",
                        "description": "Optional. The key of a document listed on the case."},
            },
            "required": ["applicantId"],
        },
    },
    "rekognition_compare_faces": {
        "name": "rekognition_compare_faces",
        "description": (
            "Compare the portrait on the applicant's identity document against their liveness "
            "reference frame. Both images are taken from the case record. Returns a boolean match "
            "and a bucketed similarity band (HIGH/MEDIUM/LOW) and records the result on the case "
            "record. The threshold is fixed server side and the raw similarity score is never "
            "returned."),
        "inputSchema": {
            "type": "object",
            "properties": {"applicantId": _APPLICANT},
            "required": ["applicantId"],
        },
    },
    "velocity_check": {
        "name": "velocity_check",
        "description": (
            "Count recent applications from the same device or IP address as this applicant, "
            "using identifiers captured at intake, and record the result on the case record. "
            "Returns counts and boolean signals, or performed=false when no identifiers were "
            "captured. Signals only — whether they amount to fraud is a decision-engine rule."),
        "inputSchema": {
            "type": "object",
            "properties": {"applicantId": _APPLICANT},
            "required": ["applicantId"],
        },
    },
    "sanctions_screen": {
        "name": "sanctions_screen",
        "description": (
            "Screen the applicant's declared identity against the consolidated sanctions list, "
            "including any published list update. Matches exact names and variants: "
            "transliteration (Mohammed/Muhammad), diacritics (José/Jose), reversed name order and "
            "married/maiden names. Every alert is classified CONFIRMED, PROBABLE or POSSIBLE with "
            "a reason, and every exclusion is reported with its reason. A name alone never "
            "confirms; two known dates of birth that differ exclude. Records the result on the "
            "case record. If the list cannot be reached it returns UNAVAILABLE, which must never "
            "be read as 'no match'."),
        "inputSchema": {
            "type": "object",
            "properties": {"applicantId": _APPLICANT, "name": _AGENT_NAME},
            "required": ["applicantId", "name"],
        },
    },
    "pep_lookup": {
        "name": "pep_lookup",
        "description": (
            "Politically exposed person check of the applicant on the case record, including "
            "exposure by association. Returns pepStatus SELF, ASSOCIATE or NONE, and for "
            "associates the relationship type (SPOUSE, CHILD, BUSINESS_PARTNER). Records the "
            "result on the case record."),
        "inputSchema": {
            "type": "object",
            "properties": {"applicantId": _APPLICANT, "name": _AGENT_NAME},
            "required": ["applicantId", "name"],
        },
    },
    "adverse_media": {
        "name": "adverse_media",
        "description": (
            "Search adverse media for the applicant on the case record and attempt entity "
            "disambiguation. Articles are only attributed to the applicant when an attribute "
            "beyond the name agrees. Returns attributed articles, rejected name collisions with "
            "reasons, and a maximum severity, and records the result on the case record. A "
            "shared name is not evidence."),
        "inputSchema": {
            "type": "object",
            "properties": {"applicantId": _APPLICANT, "name": _AGENT_NAME},
            "required": ["applicantId", "name"],
        },
    },
    # --- output contracts (R11.13) -------------------------------------------------------
    #
    # Every field is `required` where the contract depends on it, so a partial finding is rejected
    # by schema validation rather than arriving as a silent gap. No numeric constraints: the
    # Gateway's inline MCP schema rejects them (12 failed targets), so bounds are checked in the
    # tool instead — see `_submit`.
    "submit_document_analysis": {
        "name": "submit_document_analysis",
        "description": (
            "Report the result of document analysis. This is the ONLY way to record a document "
            "finding — text in your reply is not read by the decision engine. Call this exactly "
            "once, last, after every other check you intend to make."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "confidence": {
                    "type": "number",
                    "description": "Overall extraction confidence, 0.0 to 1.0 inclusive."},
                "documentType": {"type": "string",
                                 "description": "PASSPORT, DRIVING_LICENCE, NATIONAL_ID."},
                "issuingCountry": {"type": "string",
                                   "description": "ISO 3166-1 alpha-2 country code."},
                "expired": {"type": "boolean",
                            "description": "True if the expiry date has passed (R28.1)."},
                "screenPhotograph": {
                    "type": "boolean",
                    "description": "True if this appears to be a photograph of a display "
                                   "rather than the document itself (R28.3)."},
                "qualityDefects": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Named defects, e.g. blurred, glare, cropped (R28.4)."},
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
            "Report the sanctions screening result. This is the ONLY way to record a screening "
            "finding. Call this exactly once, last. If you could not complete the screening, set "
            "determined=false rather than guessing matched."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "determined": {
                    "type": "boolean",
                    "description": "False if screening could not be completed. When false the "
                                   "case goes to a human — which is correct. Never report "
                                   "matched=false to mean 'I could not check'."},
                "matched": {"type": "boolean",
                            "description": "True if any list entry alerted."},
                "hitCount": {"type": "number",
                             "description": "Count of alerts. 0 when clean."},
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
    "submit_briefing": {
        "name": "submit_briefing",
        "description": (
            "Report the reviewer briefing you wrote. This is the ONLY way to record it. Call this "
            "exactly once, last. A briefing explains the evidence; it never recommends an "
            "outcome."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "determined": {"type": "boolean",
                               "description": "False if the evidence could not be read."},
                "briefing": {"type": "string",
                             "description": "The plain-language briefing for the reviewer."},
                "openQuestions": {"type": "array", "items": {"type": "string"},
                                  "description": "What the reviewer should check."},
            },
            "required": ["determined", "briefing"],
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
                    "description": "NONE, LOW, MEDIUM or HIGH."},
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
                "limitsExplained": {
                    "type": "boolean",
                    "description": "True if the message states which limits apply. Never the "
                                   "reason for them."},
            },
            "required": ["determined", "message"],
        },
    },
}

assert set(SCHEMAS) == set(TARGET_NAMES), set(SCHEMAS) ^ set(TARGET_NAMES)
#: Everything that is *built*, whether or not it is deployed. The contract tests assert against
#: these so the implementation stays verified while the flag is off — otherwise turning it back on
#: would mean re-earning confidence in code that had quietly rotted.
ALL_SCHEMAS: dict[str, Any] = dict(SCHEMAS)
ALL_TARGET_NAMES: dict[str, str] = dict(TARGET_NAMES)

# Contract tools are removed from the registries when disabled, so no Gateway target is created for
# them and no agent can see them. Filtering here rather than at each consumer keeps one switch.
if not ENABLE_OUTPUT_CONTRACTS:
    _contract_names = set(ALL_OUTPUT_CONTRACTS.values())
    SCHEMAS = {k: v for k, v in SCHEMAS.items() if k not in _contract_names}
    TARGET_NAMES = {k: v for k, v in TARGET_NAMES.items() if k not in _contract_names}

#: The six evidence tools, always. Plus seven contracts when enabled.
EVIDENCE_TOOL_COUNT = 6
_EXPECTED_TOOLS = EVIDENCE_TOOL_COUNT + (7 if ENABLE_OUTPUT_CONTRACTS else 0)
assert len(SCHEMAS) == _EXPECTED_TOOLS, (len(SCHEMAS), _EXPECTED_TOOLS)
assert len(TARGET_NAMES) == _EXPECTED_TOOLS
assert len(ALL_OUTPUT_CONTRACTS) == 7
assert len(OUTPUT_CONTRACTS) == (7 if ENABLE_OUTPUT_CONTRACTS else 0)
assert set(OUTPUT_CONTRACTS.values()) <= set(SCHEMAS)
assert len([t for t in SCHEMAS
            if t not in set(OUTPUT_CONTRACTS.values())]) == EVIDENCE_TOOL_COUNT
# Every evidence tool requires the applicantId: it is the only input a tool takes from an agent
# that decides what gets checked.
for _tool in AGENT_TOOL_ALIASES.values():
    assert "applicantId" in SCHEMAS[_tool]["inputSchema"]["required"], _tool


def inline_payload(tool: str) -> list[dict[str, Any]]:
    """The `toolSchema.inlinePayload` value for a Lambda gateway target."""
    return [SCHEMAS[tool]]


if __name__ == "__main__":
    import json
    for t in sorted(SCHEMAS):
        print("%-28s -> Cedar action %s" % (t, cedar_action(t)))
    print()
    print(json.dumps(SCHEMAS["sanctions_screen"], indent=2))
