import type {
  AuditActor,
  PolicyDecisionResult,
  PromiseToPayStatus,
  RecoveryActionStatus,
  RecoveryCaseStatus,
  RiskLevel,
} from "./types";

export type BadgeTone = "gray" | "blue" | "violet" | "amber" | "orange" | "red" | "green";

export const TONE_CLASSES: Record<BadgeTone, string> = {
  gray: "bg-slate-100 text-slate-700 ring-slate-600/10",
  blue: "bg-blue-50 text-blue-700 ring-blue-600/20",
  violet: "bg-violet-50 text-violet-700 ring-violet-600/20",
  amber: "bg-amber-50 text-amber-800 ring-amber-600/20",
  orange: "bg-orange-50 text-orange-700 ring-orange-600/20",
  red: "bg-rose-50 text-rose-700 ring-rose-600/20",
  green: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
};

export function riskTone(level: RiskLevel | null | undefined): BadgeTone {
  switch (level) {
    case "LOW":
      return "green";
    case "MEDIUM":
      return "amber";
    case "HIGH":
      return "red";
    default:
      return "gray";
  }
}

export function caseStatusTone(status: RecoveryCaseStatus): BadgeTone {
  switch (status) {
    case "OPEN":
      return "blue";
    case "MONITORING":
      return "violet";
    case "ESCALATED":
      return "orange";
    case "RECOVERED":
    case "CLOSED":
      return "green";
    case "CLOSED_UNRECOVERED":
      return "gray";
    default:
      return "gray";
  }
}

export function actionStatusTone(status: RecoveryActionStatus): BadgeTone {
  switch (status) {
    case "EXECUTED":
      return "green";
    case "POLICY_APPROVED":
      return "blue";
    case "POLICY_REJECTED":
      return "red";
    case "FAILED":
      return "red";
    default:
      return "gray";
  }
}

export function policyDecisionTone(decision: PolicyDecisionResult): BadgeTone {
  switch (decision) {
    case "APPROVED":
      return "green";
    case "REJECTED":
      return "red";
    case "REQUIRES_HUMAN_REVIEW":
      return "amber";
    default:
      return "gray";
  }
}

export function promiseStatusTone(status: PromiseToPayStatus): BadgeTone {
  switch (status) {
    case "PENDING":
      return "blue";
    case "FULFILLED":
      return "green";
    case "BROKEN":
    case "EXPIRED":
      return "red";
    default:
      return "gray";
  }
}

// Matches app/services/policy_engine.py's RULE_* constants (persisted on
// PolicyDecision.rule) — keep this switch in sync with that module.
export function policyRuleTone(rule: string): BadgeTone {
  switch (rule) {
    case "broken_promise_forced_escalate":
      return "red";
    case "high_value_overdue_forced_escalate":
    case "reminder_cap_exceeded":
      return "orange";
    case "escalated_suppresses_reminder":
      return "violet";
    case "cooldown_not_elapsed":
    case "high_value_review":
      return "amber";
    case "reminder_approved":
      return "green";
    default:
      return "gray";
  }
}

export function actorTone(actor: AuditActor): BadgeTone {
  switch (actor) {
    case "AI_AGENT":
      return "violet";
    case "POLICY_ENGINE":
      return "blue";
    case "HUMAN":
      return "amber";
    default:
      return "gray";
  }
}
