"use client";
import {useRouter} from "next/navigation";
import {useState} from "react";

type Ledger="kaveri"|"public-2016";

export default function ExploreShowcaseButton({ledger="kaveri",label="Explore the product →"}:{ledger?:Ledger;label?:string}){
  const router=useRouter();
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  async function enter(){
    if(busy)return;
    setBusy(true);setError("");
    try{
      // Never replace a real customer's existing workspace with a showcase
      // session just because they clicked Explore; a visitor can switch ledgers.
      const me=await fetch("/api/portal/me",{cache:"no-store"});
      const kind=me.ok?(await me.json().catch(()=>({}))).kind:null;
      if(kind!=="customer"){
        const response=await fetch("/api/portal/showcase?ledger="+ledger,{method:"POST"});
        if(!response.ok){
          const payload=await response.json().catch(()=>({}));
          throw new Error(typeof payload.detail==="string"?payload.detail:"Showcase unavailable");
        }
      }
      // Start the data the next pages need now, so later clicks find warm caches instead of waiting.
      for(const path of ["follow-ups","overview","recovery-summary","invoices?q=&page=1&page_size=25&sort=posting_desc"])
        void fetch("/api/portal/"+path,{cache:"no-store"}).catch(()=>{});
      for(const page of ["/invoices","/cases","/approvals","/assistant"])router.prefetch(page);
      router.push("/follow-ups");
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
      {busy?"Opening the workspace…":label}
    </button>
    {error&&<p role="alert" style={{margin:0,fontSize:13,color:"#a32929"}}>{error}</p>}
  </div>;
}
