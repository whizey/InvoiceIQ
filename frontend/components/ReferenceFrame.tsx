"use client";
import { useEffect, useState } from "react";
type Page = "overview" | "invoices" | "cases" | "approvals";
export default function ReferenceFrame({ page }: { page: Page }) {
  const [query, setQuery] = useState<string | null>(null);
  const [syntheticDemo, setSyntheticDemo] = useState(false);
  useEffect(() => {
    const sync = () => setQuery(window.location.search);
    sync();
    window.addEventListener("popstate", sync);
    fetch("/api/demo/ready", { cache: "no-store" })
      .then((response) => { if (response.ok) setSyntheticDemo(true); })
      .catch(() => {});
    return () => window.removeEventListener("popstate", sync);
  }, []);

  if (query === null) return <p role="status" style={{padding:24,color:"#666"}}>Loading workspace…</p>;

  return <>
    <iframe title={`InvoiceIQ ${page}`} className="reference-frame" src={`/reference/${page}/index.html${query}`} />
    {syntheticDemo && <p role="status" style={{position:"fixed",right:12,bottom:12,zIndex:1000,margin:0,padding:"6px 10px",borderRadius:6,background:"#171717",color:"#fff",fontSize:12,boxShadow:"0 1px 6px #0005"}}>
      Synthetic demo · actions simulated · AI fallback
    </p>}
  </>;
}
