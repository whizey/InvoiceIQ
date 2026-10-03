// Mirrors backend/app/schemas/*.py. Money/decimal fields come back as
// strings from FastAPI (Pydantic's default Decimal JSON encoding), so
// they're typed as `string` here and parsed at render time — see format.ts.

export type CompanySegment = "SMB" | "MID_MARKET" | "ENTERPRISE";

export interface Company {
  id: string;
  name: string;
  industry: string | null;
  segment: CompanySegment;
  created_at: string;
}

export interface Contact {
  id: string;
  name: string;
  email: string;
  phone: string | null;
  role: string | null;
  is_primary: boolean;
}

export interface CompanyDetail extends Company {
  contacts: Contact[];
}

export type InvoiceStatus =
  | "DRAFT"
  | "SENT"
  | "PAID"
  | "PARTIALLY_PAID"
  | "OVERDUE"
  | "WRITTEN_OFF"
  | "CANCELLED";

export interface Invoice {
  id: string;
  invoice_number: string;
  amount_total: string;
  amount_paid: string;
  currency: string;
  issue_date: string;
  due_date: string;
  status: InvoiceStatus;
  company: Company;
  created_at: string;
  updated_at: string;
}

export type RiskLevel = "LOW" | "MEDIUM" | "HIGH";

export type RiskContributionDirection =
  | "increases_recovery_probability"
  | "decreases_recovery_probability";

export interface RiskFeatureContribution {
  feature: string;
  label: string;
  value: number;
  shap_value: number;
  direction: RiskContributionDirection;
}

export interface RiskExplanation {
  risk_score: number;
  risk_level: RiskLevel;
  recovery_probability: number;
  base_value: number;
  contributions: RiskFeatureContribution[];
}

export type RecoveryCaseStatus =
  | "OPEN"
  | "MONITORING"
  | "ESCALATED"
  | "RECOVERED"
  | "CLOSED_UNRECOVERED"
  | "CLOSED";

export type RecoveryActionType =
  | "SEND_EMAIL"
  | "SEND_PAYMENT_LINK"
  | "TRACK_PROMISE_TO_PAY"
  | "ESCALATE"
  | "WAIT"
  | "CLOSE_CASE"
  | "PLACE_VOICE_CALL";

export type RecoveryActionStatus =
  | "PROPOSED"
  | "POLICY_APPROVED"
  | "POLICY_REJECTED"
  | "EXECUTED"
  | "FAILED";

export type PolicyDecisionResult = "APPROVED" | "REJECTED" | "REQUIRES_HUMAN_REVIEW";

export interface PolicyDecision {
  id: string;
  policy_name: string;
  decision: PolicyDecisionResult;
  reason: string;
  rule: string | null;
  evaluated_at: string;
}

export interface RecoveryAction {
  id: string;
  action_type: RecoveryActionType;
  recommended_action_type: RecoveryActionType | null;
  status: RecoveryActionStatus;
  proposed_by: "AI" | "SYSTEM" | "HUMAN";
  sequence_number: number;
  executed_at: string | null;
  result: Record<string, unknown> | null;
  created_at: string;
  policy_decisions: PolicyDecision[];
}

export type AgentDecisionStage = "DIAGNOSIS" | "INTERVENTION_RECOMMENDATION";

export interface AgentDecision {
  id: string;
  stage: AgentDecisionStage;
  model_name: string;
  output: Record<string, unknown>;
  rationale: string;
  created_at: string;
}

export type PromiseToPayStatus = "PENDING" | "FULFILLED" | "BROKEN" | "EXPIRED";

export interface PromiseToPay {
  id: string;
  promised_amount: string;
  promised_date: string;
  status: PromiseToPayStatus;
  fulfilled_at: string | null;
}

export interface CommunicationLog {
  id: string;
  channel: "EMAIL" | "SMS" | "VOICE";
  direction: "OUTBOUND" | "INBOUND";
  subject: string | null;
  body: string | null;
  status: "SENT" | "FAILED" | "SIMULATED";
  recipient_email: string | null;
  sent_at: string | null;
}

export interface SendReminderEmailResult {
  status: "SENT" | "SIMULATED" | "REJECTED";
  to: string | null;
  sent_at: string | null;
  policy_decision: PolicyDecisionResult;
  reason: string;
}

export interface VoiceOutcome {
  action_type: RecoveryActionType;
  policy_decision: PolicyDecisionResult;
  reason: string;
}

export interface VoiceStartResult {
  started: boolean;
  turn_number: number;
  agent_line: string | null;
  agent_audio_base64: string | null;
  ended: boolean;
  reason: string | null;
}

export interface VoiceTurnResult {
  turn_number: number;
  transcript_user: string;
  agent_line: string | null;
  agent_audio_base64: string | null;
  ended: boolean;
  outcome: VoiceOutcome | null;
}

export type AuditActor = "SYSTEM" | "AI_AGENT" | "POLICY_ENGINE" | "HUMAN";

export interface AuditLog {
  id: string;
  entity_type: string;
  entity_id: string;
  event_type: string;
  actor: AuditActor;
  description: string;
  occurred_at: string;
}

export interface RecoveryCaseListItem {
  id: string;
  company_name: string;
  invoice_number: string;
  amount_total: string;
  days_overdue: number;
  status: RecoveryCaseStatus;
  risk_level: RiskLevel | null;
  recovery_probability: string | null;
  current_action: RecoveryActionType | null;
  recovered_amount: string;
}

export interface RecoveryCaseDetail {
  id: string;
  status: RecoveryCaseStatus;
  opened_at: string;
  closed_at: string | null;
  revenue_at_risk: string;
  recovered_amount: string;
  risk_score: string | null;
  risk_level: RiskLevel | null;
  recovery_probability: string | null;
  recovery_window_deadline: string | null;
  invoice: Invoice;
  actions: RecoveryAction[];
  agent_decisions: AgentDecision[];
  promises_to_pay: PromiseToPay[];
  communication_logs: CommunicationLog[];
  audit_logs: AuditLog[];
}

export interface DashboardMetrics {
  total_revenue_at_risk: string;
  total_revenue_recovered: string;
  recovery_rate: number | null;
  active_cases: number;
  escalated_cases: number;
  average_days_overdue: number | null;
  cases_by_risk_level: Record<string, number>;
}

export interface DetectionSummary {
  invoices_marked_overdue: number;
  cases_created: number;
  case_ids: string[];
  promises_fulfilled: number;
  promises_broken: number;
}

export interface PolicyOverrideExample {
  case_id: string;
  company_name: string;
  invoice_number: string;
  recommended_action_type: RecoveryActionType;
  action_type: RecoveryActionType;
  rule: string | null;
}

export interface PolicyOverrideStats {
  total_evaluated: number;
  override_count: number;
  override_rate: number | null;
  by_rule: Record<string, number>;
  examples: PolicyOverrideExample[];
}
