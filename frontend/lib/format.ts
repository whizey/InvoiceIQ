// Backend amounts are Decimal-as-string; parse then format with Indian
// digit grouping (₹18,00,000, matching the project brief's own examples).
export function formatCurrency(amount: string | number, currency = "INR"): string {
  const value = typeof amount === "string" ? Number(amount) : amount;
  if (Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency,
    maximumFractionDigits: 0,
  }).format(value);
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-IN", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-IN", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatNumber(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

const WORD_BOUNDARY = /_/g;

// Short acronyms that should stay fully capitalized rather than becoming
// "Smb" — extend this list if new enum values need the same treatment.
const ACRONYMS = new Set(["SMB", "AI"]);

export function titleCase(value: string): string {
  return value
    .split(WORD_BOUNDARY)
    .map((word) =>
      ACRONYMS.has(word.toUpperCase())
        ? word.toUpperCase()
        : word.charAt(0).toUpperCase() + word.slice(1).toLowerCase(),
    )
    .join(" ");
}

// Plain-English labels for RecoveryActionType, used where a first-time
// visitor reads the value directly (dashboard table, breakdown chart) —
// distinct from titleCase's mechanical "Send Email" for contexts that
// already read as a technical audit log (case detail's action history).
const ACTION_LABELS: Record<string, string> = {
  SEND_EMAIL: "Email reminder",
  SEND_PAYMENT_LINK: "Payment link",
  TRACK_PROMISE_TO_PAY: "Promise tracked",
  ESCALATE: "Escalated",
  WAIT: "Waiting",
  CLOSE_CASE: "Closed, unrecovered",
  PLACE_VOICE_CALL: "Voice call",
};

export function humanizeActionType(action: string): string {
  return ACTION_LABELS[action] ?? titleCase(action);
}

// One-sentence, no-jargon explanations for app/services/policy_engine.py's
// RULE_* constants — only the rules that can actually cause an override are
// listed here, since get_policy_override_stats() scopes by_rule to those.
const RULE_SENTENCES: Record<string, string> = {
  broken_promise_forced_escalate: "Customer missed a payment date they promised, so the case was escalated.",
  high_value_overdue_forced_escalate: "The invoice is large and very overdue, so it was escalated automatically.",
  escalated_suppresses_reminder: "The case was already escalated, so a further reminder was skipped.",
  reminder_cap_exceeded: "Too many reminders had already been sent, so it was escalated instead.",
  cooldown_not_elapsed: "A reminder went out too recently, so the system waited instead of sending another.",
};

export function humanizeRule(rule: string): string {
  return RULE_SENTENCES[rule] ?? titleCase(rule);
}
