/** Plain-language meanings for the business terms the portal uses, shown as hover explanations. */
export const GLOSSARY = {
  owed: "Still owed: money customers have been billed for but have not paid yet.",
  overdue: "Overdue: still unpaid after the date the customer agreed to pay by.",
  ageing: "Ageing: unpaid money grouped by how late it is (1–30, 31–60, 61–90 and over 90 days past the due date). The older it gets, the harder it is to collect.",
  late: "Why it's late: the strongest explanation on record, from a dispute, a part payment, what the customer said or promised, or how late this customer usually pays.",
  risk: "Late-payment risk: a model's estimate, from this customer's payment history and the invoice itself, of how likely the invoice is to be paid late. It is a guide, not a certainty.",
  approve: "Approve recovery: go ahead and follow up with the customer, for example with a reminder.",
  escalate: "Escalate: pass it to a manager or the account owner because normal follow-up is not working.",
  reject: "Reject: take no recovery action for now, for example because the customer disputes the invoice.",
  review: "Review: an overdue invoice waiting for someone to decide what happens next.",
  dso: "DSO (days sales outstanding): roughly how many days of sales are still unpaid. Lower is better.",
} as const;
export type GlossaryKey = keyof typeof GLOSSARY;

export const DECISION_WORDS: Record<string, string> = {
  APPROVE: "Approved for follow-up", ESCALATE: "Escalated to a manager", REJECT: "No action for now", RESET: "Back in the review queue",
};
