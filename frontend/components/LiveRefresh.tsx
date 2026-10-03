"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/** Re-fetch Server Components on an interval so scheduler ticks show up without clicking. */
export function LiveRefresh({ intervalMs = 5000 }: { intervalMs?: number }) {
  const router = useRouter();

  useEffect(() => {
    const id = window.setInterval(() => {
      router.refresh();
    }, intervalMs);
    return () => window.clearInterval(id);
  }, [router, intervalMs]);

  return (
    <span className="text-xs text-emerald-600">
      View refreshes every {Math.round(intervalMs / 1000)}s
    </span>
  );
}
