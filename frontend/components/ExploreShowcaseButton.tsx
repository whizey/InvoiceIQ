"use client";
import {useRouter} from "next/navigation";
import {useState} from "react";

export default function ExploreShowcaseButton(){
  const router=useRouter();
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  async function enter(){
    if(busy)return;
    setBusy(true);setError("");
    try{
      // Never replace a real customer's existing workspace with a showcase
      // session just because they clicked Explore.
      const me=await fetch("/api/portal/me",{cache:"no-store"});
      if(!me.ok){
        const response=await fetch("/api/portal/showcase",{method:"POST"});
        if(!response.ok){
          const payload=await response.json().catch(()=>({}));
          throw new Error(typeof payload.detail==="string"?payload.detail:"Showcase unavailable");
        }
      }
      router.push("/workspace-dashboard");
      router.refresh();
    }catch(e){
      setError(e instanceof Error?e.message:"Could not open the showcase");
      setBusy(false);
    }
  }
  return <div style={{display:"grid",gap:9}}>
    <button type="button" disabled={busy} onClick={enter}
      style={{border:"1px solid #999",borderRadius:8,background:"white",color:"#171717",
              padding:"15px 22px",font:"inherit",fontWeight:600,cursor:busy?"wait":"pointer"}}>
      {busy?"Opening your workspace…":"Explore the product →"}
    </button>
    {error&&<p role="alert" style={{margin:0,fontSize:13,color:"#a32929"}}>{error}</p>}
  </div>;
}
