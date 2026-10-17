"use client";
import { useEffect, useRef, useState } from "react";
import {useRouter} from "next/navigation";

type Page = "overview" | "invoices" | "cases" | "approvals";

export default function ReferenceFrame({ page, portal = false }: { page: Page; portal?: boolean }) {
  const router=useRouter();
  const iframeRef=useRef<HTMLIFrameElement>(null);
  const [query, setQuery] = useState<string | null>(null);

  useEffect(() => {
    const sync = () => {
      const params = new URLSearchParams(window.location.search);
      params.delete("scenario");
      if (portal) params.set("portal", "1");
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
  }, [portal]);

  // Turn same-origin sidebar links into real top-level Next.js navigation
  // so Back works. Do not rewrite the preserved reference HTML or its KPIs.
  useEffect(()=>{
    if(!portal||query===null)return;
    const iframe=iframeRef.current;
    if(!iframe)return;
    let detach=()=>{};
    const attach=()=>{
      detach();
      const doc=iframe.contentDocument;
      if(!doc)return;
      if(page==="overview"&&!doc.getElementById("portal-kpi-spacing")){
        const style=doc.createElement("style");
        style.id="portal-kpi-spacing";
        style.textContent='@media (min-width: 900px) and (max-width: 1700px) { section[aria-label="Key metrics"] { grid-template-columns: repeat(3, minmax(0, 1fr)) !important; } } @media (max-width: 760px) { nav[aria-label="Primary"] { display: none !important; } }';
        doc.head.append(style);
      }
      // Size the overview frame to its content so the dashboard and the recovery
      // section below it read as one page with a single scrollbar.
      let observer:ResizeObserver|undefined;
      if(page==="overview"){
        const fit=()=>{const h=doc.documentElement.scrollHeight;if(h>0)iframe.style.height=h+"px"};
        fit();
        observer=new ResizeObserver(fit);
        observer.observe(doc.documentElement);
        observer.observe(doc.body);
      }
      const navigate=(event:MouseEvent)=>{
        if(event.defaultPrevented||event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
        const element=event.target;
        if(!element||!("closest" in element))return;
        const anchor=(element as Element).closest("a[href]");
        if(!anchor||anchor.getAttribute("target")==="_blank")return;
        const href=anchor.getAttribute("href")||"";
        if(!href.startsWith("/")||href.startsWith("//"))return;
        const destination=new URL(href,window.location.origin);
        if(destination.origin!==window.location.origin)return;
        if(!["/","/invoices","/cases","/approvals"].includes(destination.pathname))return;
        event.preventDefault();
        event.stopPropagation();
        destination.searchParams.delete("portal");
        router.push((destination.pathname==="/"?"/workspace-dashboard":destination.pathname)+destination.search+destination.hash);
      };
      doc.addEventListener("click",navigate,true);
      detach=()=>{doc.removeEventListener("click",navigate,true);observer?.disconnect()};
    };
    iframe.addEventListener("load",attach);
    attach();
    return()=>{detach();iframe.removeEventListener("load",attach)};
  },[page,portal,query,router]);

  if (query === null) {
    return <p role="status" style={{ padding:24, color:"#666" }}>Loading dashboard…</p>;
  }

  return <iframe
    ref={iframeRef}
    title={`InvoiceIQ ${page}`}
    className="reference-frame"
    src={`/reference/${page}/index.html${query}`}
  />;
}
