"use client";
import {useRouter,usePathname} from "next/navigation";
import Link from "next/link";
import {useState} from "react";

/** Shared top navigation for the authenticated SaaS area.
 * Browser history is retained between pages; the back fallback handles
 * directly opened deep links with no same-origin referring page.
 */
export default function PortalNavigation(){
  const router=useRouter();
  const pathname=usePathname();
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
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
  const destinations=[
    {href:"/workspace-dashboard",label:"Analytics"},
    {href:"/invoices",label:"Invoices"},
    {href:"/cases",label:"Cases"},
    {href:"/approvals",label:"Approvals"},
    {href:"/assistant",label:"AI assistant"},
    {href:"/settings",label:"Settings"},
  ];
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
    <div style={{display:"flex",alignItems:"center",gap:6,flexWrap:"wrap"}}>
      {destinations.map(link=><Link key={link.href} href={link.href}
        aria-current={pathname===link.href?"page":undefined}
        style={{padding:"8px 11px",borderRadius:7,fontSize:13,textDecoration:"none",
          color:pathname===link.href?"#fff":"#444",background:pathname===link.href?"#161616":"transparent"}}>
        {link.label}</Link>)}
      <button type="button" disabled={busy} onClick={signOut}
        style={{marginLeft:6,border:"1px solid #ddd",borderRadius:7,background:"white",padding:"8px 10px",cursor:"pointer"}}>
        {busy?"Signing out…":"Sign out"}</button>
    </div>
    {error&&<span role="alert" style={{color:"#922b1e",fontSize:12}}>{error}</span>}
  </nav>
  </>;
}
