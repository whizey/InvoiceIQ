export function MetricCard({
  label,
  value,
  caption,
  tone = "default",
}: {
  label: string;
  value: string;
  caption?: string;
  tone?: "default" | "danger" | "success";
}) {
  const valueColor =
    tone === "danger" ? "text-rose-600" : tone === "success" ? "text-emerald-600" : "text-slate-900";

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <p className="text-sm font-medium text-slate-500">{label}</p>
      <p className={`mt-2 text-3xl font-semibold tabular-nums tracking-tight ${valueColor}`}>
        {value}
      </p>
      {caption && <p className="mt-1 text-xs text-slate-400">{caption}</p>}
    </div>
  );
}
