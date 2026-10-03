import { TONE_CLASSES, type BadgeTone } from "@/lib/badges";
import { titleCase } from "@/lib/format";

const BAR_TONE: Record<BadgeTone, string> = {
  gray: "bg-slate-300",
  blue: "bg-blue-500",
  violet: "bg-violet-500",
  amber: "bg-amber-500",
  orange: "bg-orange-500",
  red: "bg-rose-500",
  green: "bg-emerald-500",
};

export function BreakdownList({
  title,
  items,
}: {
  title: string;
  items: { label: string; count: number; tone: BadgeTone }[];
}) {
  const total = items.reduce((sum, i) => sum + i.count, 0);

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <p className="text-sm font-medium text-slate-500">{title}</p>
      <div className="mt-4 space-y-3">
        {total === 0 && <p className="text-sm text-slate-400">No data yet.</p>}
        {items
          .filter((i) => i.count > 0)
          .map((item) => (
            <div key={item.label} className="flex items-center gap-3">
              <span
                className={`w-32 shrink-0 truncate text-xs font-medium ${TONE_CLASSES[item.tone].split(" ")[1]}`}
              >
                {titleCase(item.label)}
              </span>
              <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
                <div
                  className={`h-full rounded-full ${BAR_TONE[item.tone]}`}
                  style={{ width: `${total ? (item.count / total) * 100 : 0}%` }}
                />
              </div>
              <span className="w-6 shrink-0 text-right text-xs font-semibold tabular-nums text-slate-600">
                {item.count}
              </span>
            </div>
          ))}
      </div>
    </div>
  );
}
