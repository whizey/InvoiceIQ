"use client";
import {useRouter,usePathname} from "next/navigation";
import Link from "next/link";
import {useEffect,useRef,useState} from "react";

/** Shared top navigation for the authenticated SaaS area.
 * Browser history is retained between pages; the back fallback handles
 * directly opened deep links with no same-origin referring page.
 *
 * Follow-ups, Invoices and Settings are the job most visitors came for, so they stay on the bar. Everything
 * else (the dashboard, review queues, the assistant, the model comparison) still works exactly the same, just
 * one click further under "More", so the bar doesn't read like eight equally-important destinations.
 */
export default function PortalNavigation(){
  const router=useRouter();
  const pathname=usePathname();
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [moreOpen,setMoreOpen]=useState(false);
  const moreRef=useRef<HTMLDivElement>(null);
  const [shownFor,setShownFor]=useState(pathname);
  if(pathname!==shownFor){setShownFor(pathname);setMoreOpen(false)} // close the menu when a link in it navigates
  function back(){
    let sameOrigin=false;
    try{sameOrigin=Boolean(document.referrer&&new URL(document.referrer).origin===location.origin)}
    catch{sameOrigin=false}
    if(sameOrigin||window.history.length>1)router.back();
    else router.push("/home");
  }
  async function signOut(){
    if(busy)return;setBusy(true);setError("");
    try{
      const response=await fetch("/api/portal/session",{method:"DELETE"});
      if(!response.ok)throw new Error("Sign out failed");
      router.push("/home");router.refresh();
    }catch{setError("Sign out failed. Please try again.");setBusy(false)}
  }
  useEffect(()=>{
    if(!moreOpen)return;
    function onDown(e:MouseEvent){if(moreRef.current&&!moreRef.current.contains(e.target as Node))setMoreOpen(false)}
    function onKey(e:KeyboardEvent){if(e.key==="Escape")setMoreOpen(false)}
    document.addEventListener("mousedown",onDown);document.addEventListener("keydown",onKey);
    return()=>{document.removeEventListener("mousedown",onDown);document.removeEventListener("keydown",onKey)};
  },[moreOpen]);
  const primary=[
    {href:"/follow-ups",label:"Follow-ups"},
    {href:"/invoices",label:"Invoices"},
    {href:"/settings",label:"Settings"},
  ];
  const more=[
    {href:"/workspace-dashboard",label:"Analytics"},
    {href:"/cases",label:"Cases"},
    {href:"/approvals",label:"Approvals"},
    {href:"/assistant",label:"AI assistant"},
    {href:"/model",label:"Model"},
  ];
  const linkStyle=(active:boolean):React.CSSProperties=>({padding:"8px 11px",borderRadius:7,fontSize:13,textDecoration:"none",
    color:active?"#fff":"#444",background:active?"#161616":"transparent"});
  const inMore=more.some(link=>link.href===pathname);
  return <><nav aria-label="Workspace navigation" style={{
    minHeight:56,background:"#fff",borderBottom:"1px solid #dededb",
    display:"flex",alignItems:"center",justifyContent:"space-between",
    padding:"9px clamp(15px,3.3vw,52px)",gap:14,flexWrap:"wrap",fontFamily:"Arial,sans-serif"
  }}>
    <div style={{display:"flex",alignItems:"center",gap:12}}>
      <button type="button" onClick={back} aria-label="Go back to previous page"
        style={{border:"1px solid #d4d4d1",background:"#f7f7f5",borderRadius:8,padding:"9px 12px",cursor:"pointer",fontWeight:600}}>
        ← Back
      </button>
      <Link href="/home" style={{textDecoration:"none",color:"#161616",fontWeight:800,letterSpacing:"-.04em"}}>▣ InvoiceIQ</Link>
    </div>
    <div className="pn-links" style={{display:"flex",alignItems:"center",gap:6,flexWrap:"wrap"}}>
      {primary.map(link=><Link key={link.href} href={link.href}
        aria-current={pathname===link.href?"page":undefined}
        style={linkStyle(pathname===link.href)}>{link.label}</Link>)}
      <div ref={moreRef} style={{position:"relative"}}>
        <button type="button" aria-haspopup="true" aria-expanded={moreOpen} onClick={()=>setMoreOpen(v=>!v)}
          style={{...linkStyle(inMore||moreOpen),border:0,cursor:"pointer",font:"inherit",display:"flex",alignItems:"center",gap:4}}>
          More <span aria-hidden="true" style={{fontSize:10}}>{moreOpen?"▲":"▼"}</span>
        </button>
        {moreOpen&&<div aria-label="More destinations" style={{
          position:"absolute",top:"calc(100% + 6px)",right:0,background:"#fff",border:"1px solid #dededb",
          borderRadius:9,boxShadow:"0 10px 28px rgba(0,0,0,.12)",padding:6,display:"grid",gap:2,minWidth:160,zIndex:30}}>
          {more.map(link=><Link key={link.href} href={link.href}
            aria-current={pathname===link.href?"page":undefined}
            style={{...linkStyle(pathname===link.href),display:"block",whiteSpace:"nowrap"}}>{link.label}</Link>)}
        </div>}
      </div>
      <button type="button" disabled={busy} onClick={signOut}
        style={{marginLeft:6,border:"1px solid #ddd",borderRadius:7,background:"white",padding:"8px 10px",cursor:"pointer"}}>
        {busy?"Signing out…":"Sign out"}</button>
    </div>
    {error&&<span role="alert" style={{color:"#922b1e",fontSize:12}}>{error}</span>}
  </nav>
  </>;
}
