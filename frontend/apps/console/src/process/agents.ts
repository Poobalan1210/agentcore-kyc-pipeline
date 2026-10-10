/**
 * What the console says about each agent: its name on screen, and the tools its AgentCore harness
 * may call. Mirrors `infra/kyc/config.py` (AGENTS) and `infra/kyc/harnesses_stack.py`
 * (AllowedTools): no route returns them, so they are written down once, here.
 */
export interface AgentInfo {
  name: string;
  /** What the lane shows under the agent: its Gateway tool and what that calls, or "no tools". */
  tool: string;
  /** The Gateway tools in its AllowedTools: every call to them is evaluated by Cedar. */
  gateway: string[];
  /** AgentCore built-ins in its AllowedTools. They are not reached through the Gateway, so Cedar
   * does not see them. */
  builtin?: string[];
  /** What a tool-less agent is given instead (its prompt's input). */
  given?: string;
}

export const AGENTS: Record<string, AgentInfo> = {
  documents: {
    name: "Documents agent",
    tool: "s3_get_document → Amazon Textract",
    gateway: ["s3_get_document"],
  },
  identity: {
    name: "Identity agent",
    tool: "rekognition_compare_faces",
    gateway: ["rekognition_compare_faces"],
  },
  // Code Interpreter is an AgentCore built-in, not a Gateway tool (harnesses_stack.py).
  fraud: {
    name: "Fraud agent",
    tool: "velocity_check",
    gateway: ["velocity_check"],
    builtin: ["Code Interpreter"],
  },
  prescreen: {
    name: "Pre-Screen agent",
    tool: "sanctions_screen",
    gateway: ["sanctions_screen"],
  },
  compliance: {
    name: "Compliance agent",
    tool: "pep_lookup, adverse_media",
    gateway: ["pep_lookup", "adverse_media"],
  },
  notify: {
    name: "Notify agent",
    tool: "no tools · message → tipping-off guard",
    gateway: [],
    given: "the account's status and limits, nothing else",
  },
  briefing: {
    name: "Briefing agent",
    tool: "no tools · explains, never decides",
    gateway: [],
    given: "the reason for review and the recorded facts",
  },
};

export function agentName(key: string): string {
  return AGENTS[key]?.name ?? key;
}

/** The rows a lane's drawer gives for what the harness may call, or is given instead. */
export function toolRows(key: string): Array<[string, string]> {
  const info = AGENTS[key];
  if (!info) return [];
  if (!info.gateway.length) {
    return [
      ["Tools", "none · no Gateway attached"],
      ...(info.given ? [["Given", info.given] as [string, string]] : []),
    ];
  }
  const rows: Array<[string, string]> = [["Gateway tools", info.gateway.join(", ")]];
  for (const name of info.builtin ?? []) {
    rows.push(["Built-in tool", `${name} · AgentCore built-in, outside the Gateway, so Cedar does not see it`]);
  }
  return rows;
}

/** `harnessVersion` on a decision record is `agent=version,…` for every harness (`infra/app.py`). */
export function harnessVersionOf(pins: string | null | undefined, key: string): string | null {
  if (!pins) return null;
  for (const pair of pins.split(",")) {
    const [agent, version] = pair.split("=");
    if (agent?.trim() === key && version?.trim()) return version.trim();
  }
  return null;
}

/** The resource id at the end of an ARN (`…:gateway/<id>`), without the account in front of it. */
export function resourceId(arn: string | null | undefined): string | null {
  const value = (arn ?? "").split("#")[0].trim();
  if (!value) return null;
  const id = value.split("/").pop();
  return id && id !== value ? id : null;
}
