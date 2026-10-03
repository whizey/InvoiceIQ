"use client";

import { useState, useTransition } from "react";
import { sendReminderEmailAction } from "@/app/actions";
import { formatDateTime } from "@/lib/format";

const STATUS_COPY: Record<string, string> = {
  SENT: "Sent",
  SIMULATED: "Simulated — no email provider configured",
  REJECTED: "Not sent",
};

export function SendReminderEmailButton({ caseId }: { caseId: string }) {
  const [isPending, startTransition] = useTransition();
  const [message, setMessage] = useState<{ tone: "success" | "muted" | "error"; text: string } | null>(
    null,
  );

  return (
    <div className="inline-flex flex-col items-start gap-1">
      <button
        type="button"
        disabled={isPending}
        onClick={() => {
          setMessage(null);
          startTransition(async () => {
            const result = await sendReminderEmailAction(caseId);
            if (!result.ok) {
              setMessage({ tone: "error", text: result.error });
              return;
            }
            if (result.status === "REJECTED") {
              setMessage({ tone: "muted", text: `Not sent — ${result.reason}` });
              return;
            }
            const when = result.sent_at ? ` at ${formatDateTime(result.sent_at)}` : "";
            setMessage({
              tone: result.status === "SENT" ? "success" : "muted",
              text: `${STATUS_COPY[result.status]} to ${result.to}${when}`,
            });
          });
        }}
        className="rounded-lg bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm ring-1 ring-inset ring-slate-300 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400"
      >
        {isPending ? "Sending…" : "Send reminder to my inbox"}
      </button>
      {message && (
        <p
          className={`max-w-xs text-xs ${
            message.tone === "success"
              ? "text-emerald-600"
              : message.tone === "error"
                ? "text-rose-600"
                : "text-slate-500"
          }`}
        >
          {message.text}
        </p>
      )}
    </div>
  );
}
