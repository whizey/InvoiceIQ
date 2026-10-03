import { Badge } from "@/components/Badge";
import { actorTone } from "@/lib/badges";
import { formatDateTime, titleCase } from "@/lib/format";
import type { AuditLog } from "@/lib/types";

export function AuditTimeline({ entries }: { entries: AuditLog[] }) {
  if (entries.length === 0) {
    return <p className="text-sm text-slate-400">No audit history yet.</p>;
  }

  return (
    <ol className="relative space-y-6 border-l border-slate-200 pl-6">
      {entries.map((entry) => (
        <li key={entry.id} className="relative">
          <span className="absolute -left-[29px] top-1 h-3 w-3 rounded-full border-2 border-white bg-indigo-500 ring-1 ring-slate-200" />
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-slate-900">{titleCase(entry.event_type)}</span>
            <Badge tone={actorTone(entry.actor)}>{titleCase(entry.actor)}</Badge>
          </div>
          <p className="mt-0.5 text-sm text-slate-600">{entry.description}</p>
          <p className="mt-0.5 text-xs text-slate-400">{formatDateTime(entry.occurred_at)}</p>
        </li>
      ))}
    </ol>
  );
}
