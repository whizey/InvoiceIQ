"use client";

import { useState, useTransition } from "react";
import type { ActionResult } from "@/app/actions";

const VARIANT_CLASSES = {
  primary: "bg-indigo-600 text-white hover:bg-indigo-500 disabled:bg-indigo-300",
  secondary:
    "bg-white text-slate-700 ring-1 ring-inset ring-slate-300 hover:bg-slate-50 disabled:text-slate-400",
  danger:
    "bg-white text-rose-600 ring-1 ring-inset ring-rose-200 hover:bg-rose-50 disabled:text-rose-300",
} as const;

export function ActionButton({
  label,
  pendingLabel,
  onRun,
  variant = "primary",
}: {
  label: string;
  pendingLabel?: string;
  onRun: () => Promise<ActionResult>;
  variant?: keyof typeof VARIANT_CLASSES;
}) {
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  return (
    <div className="inline-flex flex-col items-start gap-1">
      <button
        type="button"
        disabled={isPending}
        onClick={() => {
          setError(null);
          startTransition(async () => {
            const result = await onRun();
            if (!result.ok) setError(result.error);
          });
        }}
        className={`rounded-lg px-4 py-2 text-sm font-medium shadow-sm transition-colors disabled:cursor-not-allowed ${VARIANT_CLASSES[variant]}`}
      >
        {isPending ? (pendingLabel ?? "Working…") : label}
      </button>
      {error && <p className="max-w-xs text-xs text-rose-600">{error}</p>}
    </div>
  );
}
