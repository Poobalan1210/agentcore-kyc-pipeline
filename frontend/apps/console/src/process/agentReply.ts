/**
 * An agent's reply, read for the screen only.
 *
 * The rules never read what an agent says (D-15): every outcome rests on the facts the tools
 * recorded. This only makes the agents' words readable, on Backstage ("Said") and in the
 * reviewer's narrative tab and briefing. Defensive on purpose: the models answer in JSON when
 * asked, but not reliably. A reply can arrive fenced in markdown, after a visible `<thinking>`
 * block (Nova Lite and Micro emit them unasked, F-68), with commentary after the object, or as
 * prose. Whatever cannot be read as fields is shown as the text it is, never dropped.
 */

export interface AgentReply {
  /** The reply as the model wrote it, less any `<thinking>` blocks. */
  text: string;
  /** Its fields, when the reply is, or contains, one JSON object. */
  fields: Record<string, unknown> | null;
  /** What the agent said, in its own words: the field its prompt asks it to write in (`notes`,
   * `summary`, `body`), else its text. Null when it said nothing readable. */
  said: string | null;
}

/** The field each prompt asks the agent to put its words in (`harnesses/prompts.py`). */
export const SAID_FIELDS = ["notes", "summary", "body"];

const THINKING = /<thinking>[\s\S]*?<\/thinking>/gi;
const FENCED = /```(?:json)?\s*([\s\S]*?)```/i;

export function readReply(raw: string | null | undefined): AgentReply | null {
  if (typeof raw !== "string") return null;
  const text = raw.replace(THINKING, "").trim();
  if (!text) return null;
  const fenced = FENCED.exec(text);
  const fields = objectIn(fenced ? fenced[1] : text);
  return { text, fields, said: fields ? saidIn(fields) : oneLine(text) };
}

export interface BriefingParts {
  summary: string | null;
  checks: Array<{ check: string; finding: string }>;
  openQuestions: string[];
}

/** The Briefing agent's fields (`harnesses/prompts.py` BRIEFING), or null when it wrote prose. */
export function readBriefing(raw: string | null | undefined): BriefingParts | null {
  const fields = readReply(raw)?.fields;
  if (!fields) return null;
  const summary = typeof fields.summary === "string" && fields.summary.trim()
    ? fields.summary.trim()
    : null;
  const checks = (Array.isArray(fields.checks) ? fields.checks : [])
    .map((item): { check: string; finding: string } | null => {
      if (typeof item === "string") return item.trim() ? { check: "", finding: item.trim() } : null;
      if (!item || typeof item !== "object") return null;
      const row = item as Record<string, unknown>;
      const check = String(row.check ?? row.name ?? "").trim();
      const finding = String(row.finding ?? row.result ?? formatValue(row)).trim();
      return finding ? { check, finding } : null;
    })
    .filter((row): row is { check: string; finding: string } => row !== null);
  const openQuestions = (Array.isArray(fields.openQuestions) ? fields.openQuestions : [])
    .map((q) => (typeof q === "string" ? q.trim() : formatValue(q)))
    .filter(Boolean);
  if (!summary && !checks.length && !openQuestions.length) return null;
  return { summary, checks, openQuestions };
}

/** "checkDigitsValid" -> "Check digits valid". */
export function fieldLabel(key: string): string {
  const words = key.replace(/_/g, " ").replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A field's value in a few words: lists joined, objects as `key: value` pairs. */
export function formatValue(value: unknown, depth = 0): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value !== "object") return String(value);
  if (depth > 1) return JSON.stringify(value);
  if (Array.isArray(value)) {
    return value.length ? value.map((v) => formatValue(v, depth + 1)).join(", ") : "none";
  }
  const entries = Object.entries(value as Record<string, unknown>);
  return entries.length
    ? entries.map(([k, v]) => `${fieldLabel(k)}: ${formatValue(v, depth + 1)}`).join("; ")
    : "—";
}

function objectIn(text: string): Record<string, unknown> | null {
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start < 0 || end <= start) return null;
  try {
    const value: unknown = JSON.parse(text.slice(start, end + 1));
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** The agent's words when it answered in fields: its prose field, else its first few fields. */
function saidIn(fields: Record<string, unknown>): string | null {
  for (const key of SAID_FIELDS) {
    const value = fields[key];
    if (typeof value === "string" && value.trim()) return oneLine(value);
  }
  const parts: string[] = [];
  for (const [key, value] of Object.entries(fields)) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value) ? value.length === 0 : typeof value === "object") continue;
    parts.push(`${fieldLabel(key)}: ${formatValue(value)}`);
    if (parts.length === 4) break;
  }
  return parts.length ? parts.join(" · ") : null;
}

function oneLine(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}
