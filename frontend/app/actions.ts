"use server";

import { revalidatePath } from "next/cache";
import {
  ApiError,
  detectOverdue,
  runRecoveryCase,
  sendReminderEmail,
  simulatePayment,
  startVoiceCall,
  submitVoiceTurn,
} from "@/lib/api";
import type { SendReminderEmailResult, VoiceStartResult, VoiceTurnResult } from "@/lib/types";

export type ActionResult = { ok: true } | { ok: false; error: string };

export type SendReminderEmailActionResult =
  | ({ ok: true } & SendReminderEmailResult)
  | { ok: false; error: string };

export type VoiceStartActionResult =
  | ({ ok: true } & VoiceStartResult)
  | { ok: false; error: string };

export type VoiceTurnActionResult =
  | ({ ok: true } & VoiceTurnResult)
  | { ok: false; error: string };

export async function runRecoveryCaseAction(caseId: string): Promise<ActionResult> {
  try {
    await runRecoveryCase(caseId);
    revalidatePath(`/cases/${caseId}`);
    revalidatePath("/");
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof ApiError ? err.message : "Something went wrong." };
  }
}

export async function detectOverdueAction(): Promise<ActionResult> {
  try {
    await detectOverdue();
    revalidatePath("/");
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof ApiError ? err.message : "Something went wrong." };
  }
}

export async function simulatePaymentAction(
  invoiceId: string,
  caseId: string,
): Promise<ActionResult> {
  try {
    await simulatePayment(invoiceId);
    revalidatePath(`/cases/${caseId}`);
    revalidatePath("/");
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof ApiError ? err.message : "Something went wrong." };
  }
}

export async function sendReminderEmailAction(
  caseId: string,
): Promise<SendReminderEmailActionResult> {
  try {
    const result = await sendReminderEmail(caseId);
    revalidatePath(`/cases/${caseId}`);
    return { ok: true, ...result };
  } catch (err) {
    return { ok: false, error: err instanceof ApiError ? err.message : "Something went wrong." };
  }
}

export async function startVoiceCallAction(caseId: string): Promise<VoiceStartActionResult> {
  try {
    const result = await startVoiceCall(caseId);
    revalidatePath(`/cases/${caseId}`);
    return { ok: true, ...result };
  } catch (err) {
    return { ok: false, error: err instanceof ApiError ? err.message : "Something went wrong." };
  }
}

export async function submitVoiceTurnAction(
  caseId: string,
  formData: FormData,
): Promise<VoiceTurnActionResult> {
  try {
    const result = await submitVoiceTurn(caseId, formData);
    revalidatePath(`/cases/${caseId}`);
    return { ok: true, ...result };
  } catch (err) {
    return { ok: false, error: err instanceof ApiError ? err.message : "Something went wrong." };
  }
}
