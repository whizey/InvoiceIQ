"use client";
import { useEffect, useState } from "react";

type Page = "overview" | "invoices" | "cases" | "approvals";

export default function ReferenceFrame({ page }: { page: Page }) {
  const [query, setQuery] = useState<string | null>(null);

  useEffect(() => {
    const sync = () => {
      const params = new URLSearchParams(window.location.search);
      params.delete("scenario");
      if (!params.has("sort")) params.set("sort", "posting_desc");
      if (!params.has("preset") && !params.has("start_date") && !params.has("end_date")) {
        const now = new Date();
        const past = new Date(now);
        past.setUTCFullYear(now.getUTCFullYear() - 1);
        params.set("start_date", past.toISOString().slice(0, 10));
        params.set("end_date", now.toISOString().slice(0, 10));
        params.set("preset", "12M");
      }
      setQuery("?" + params.toString());
    };
    sync();
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);

  if (query === null) {
    return <p role="status" style={{ padding:24, color:"#666" }}>Loading dashboard…</p>;
  }

  return <iframe
    title={`InvoiceIQ ${page}`}
    className="reference-frame"
    src={`/reference/${page}/index.html${query}`}
  />;
}
