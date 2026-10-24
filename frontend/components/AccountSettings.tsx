"use client";

import {useCallback,useEffect,useState} from "react";
import {useRouter,useSearchParams} from "next/navigation";
import Link from "next/link";
import PortalNavigation from "@/components/PortalNavigation";

type Settings={email:string;full_name:string;company:string;role:string;session_expires_at:string};
type ZohoEvent={invoice_number:string|null;outcome:"created"|"updated"|"skipped";reason:string|null;received_at:string};
type ZohoStatus={configured:boolean;connected:boolean;connected_at:string|null;last_synced_at:string|null;
  invoices_synced:number;webhook_url:string|null;api_domain:string|null;events?:ZohoEvent[]};
const ZOHO_MESSAGES:Record<string,string>={
  connected:"Zoho connected. Add the webhook URL below in Zoho Books to start streaming invoices.",
  expired:"The connection attempt expired. Try connecting again.",
  denied:"Zoho authorization was not completed.",
  error:"Zoho could not verify the authorization. Try connecting again.",
};

export default function AccountSettings(){
  const router=useRouter();
  const searchParams=useSearchParams();
  const [settings,setSettings]=useState<Settings|null>(null);
  const [zoho,setZoho]=useState<ZohoStatus|null>(null);
  const [zohoBusy,setZohoBusy]=useState(false);
  const [zohoError,setZohoError]=useState("");
  const [zohoBanner,setZohoBanner]=useState("");
  const [visitor,setVisitor]=useState(false);
  const [name,setName]=useState("");
  const [company,setCompany]=useState("");
  const [currentPassword,setCurrentPassword]=useState("");
  const [newPassword,setNewPassword]=useState("");
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [success,setSuccess]=useState("");
  const [confirmName,setConfirmName]=useState("");
  const [erasePassword,setErasePassword]=useState("");
  async function erase(scope:"data"|"account"){
    if(busy)return;
    const warning=scope==="data"?"Delete every invoice, payment, note, decision and reminder in this workspace? This cannot be undone.":
      "Delete your account and this workspace with all its data? This cannot be undone.";
    if(!window.confirm(warning))return;
    setBusy(true);setError("");setSuccess("");
    try{
      const response=await fetch("/api/portal/erase",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({scope,confirm:confirmName,password:erasePassword})});
      const result=await response.json();
      if(!response.ok)throw new Error(result.detail||"Nothing was deleted");
      if(scope==="account"){router.push("/home");router.refresh();return}
      setConfirmName("");setErasePassword("");setSuccess(`Deleted ${result.invoices} invoices and everything attached to them.`);
    }catch(e){setError(e instanceof Error?e.message:"Nothing was deleted")}
    finally{setBusy(false)}
  }
  const load=useCallback(async()=>{
    const response=await fetch("/api/portal/settings",{cache:"no-store"});
    if(response.status===401){router.replace("/home");return}
    if(response.status===403){setVisitor(true);return}
    const payload=await response.json();
    if(!response.ok)throw new Error(payload.detail||"Account settings unavailable");
    setSettings(payload);setName(payload.full_name);setCompany(payload.company);
  },[router]);
  useEffect(()=>{let active=true;(async()=>{
    try{await load()}catch(e){if(active)setError(e instanceof Error?e.message:"Account settings unavailable")}
  })();return()=>{active=false}},[load]);
  const loadZoho=useCallback(async()=>{
    const response=await fetch("/api/portal/zoho-status",{cache:"no-store"});
    if(response.ok)setZoho(await response.json());
  },[]);
  useEffect(()=>{
    if(settings&&["owner","admin"].includes(settings.role)){(async()=>{await loadZoho()})()}
  },[settings,loadZoho]);
  const zohoFlag=searchParams.get("zoho");
  const zohoReason=searchParams.get("reason");
  const [shownZohoFlag,setShownZohoFlag]=useState<string|null>(null);
  if(zohoFlag&&zohoFlag!==shownZohoFlag){
    setShownZohoFlag(zohoFlag);
    const message=ZOHO_MESSAGES[zohoFlag]||"";
    setZohoBanner(zohoFlag==="error"&&zohoReason?`${message} (${zohoReason})`:message);
  }
  useEffect(()=>{
    if(searchParams.get("zoho"))router.replace("/settings");
  },[searchParams,router]);
  async function connectZoho(){
    if(zohoBusy)return;
    setZohoBusy(true);setZohoError("");
    try{
      const response=await fetch("/api/portal/zoho-connect",{method:"POST"});
      const result=await response.json();
      if(!response.ok)throw new Error(result.detail||"Could not start the Zoho connection");
      window.location.href=result.authorize_url;
    }catch(e){setZohoError(e instanceof Error?e.message:"Could not start the Zoho connection");setZohoBusy(false)}
  }
  async function disconnectZoho(){
    if(zohoBusy)return;
    if(!window.confirm("Disconnect Zoho Books? The current webhook URL will stop working immediately."))return;
    setZohoBusy(true);setZohoError("");
    try{
      const response=await fetch("/api/portal/zoho-disconnect",{method:"POST"});
      if(!response.ok)throw new Error((await response.json()).detail||"Could not disconnect");
      await loadZoho();
    }catch(e){setZohoError(e instanceof Error?e.message:"Could not disconnect")}
    finally{setZohoBusy(false)}
  }
  async function syncNow(){
    if(zohoBusy)return;
    setZohoBusy(true);setZohoError("");setShownZohoFlag("sync");
    try{
      const response=await fetch("/api/portal/zoho-sync-now",{method:"POST"});
      const result=await response.json();
      if(!response.ok)throw new Error(result.detail||"Sync failed");
      setZohoBanner(`Found ${result.found} invoice${result.found===1?"":"s"} in Zoho: `+
        `${result.imported} imported, ${result.skipped} skipped (drafts or void).`);
      await loadZoho();
    }catch(e){setZohoError(e instanceof Error?e.message:"Sync failed")}
    finally{setZohoBusy(false)}
  }
  async function save(payload:Record<string,string>,message:string){
    if(busy)return;
    setBusy(true);setError("");setSuccess("");
    try{
      const response=await fetch("/api/portal/settings",{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
      const result=await response.json();
      if(!response.ok)throw new Error(result.detail||"Settings were not saved");
      await load();setSuccess(message);router.refresh();return true;
    }catch(e){setError(e instanceof Error?e.message:"Settings were not saved");return false}
    finally{setBusy(false)}
  }
  return <div className="settings-page">
    <PortalNavigation/>
    <main className="settings-main">
      <p className="settings-eyebrow">Workspace / Account</p>
      <h1>Settings</h1>
      <p className="settings-intro">Manage your profile, company name and password.</p>
      {error&&<div role="alert" className="settings-error">{error} <button onClick={()=>void load()}>Retry</button></div>}
      {success&&<p role="status" className="settings-success">{success}</p>}
      {visitor&&<section className="settings-card"><h2>Visitor workspace</h2><p className="settings-muted">Account settings are available when you create a company workspace.</p><Link href="/signup">Create workspace →</Link></section>}
      {!settings&&!error&&!visitor&&<p role="status">Loading settings…</p>}
      {settings&&<div className="settings-grid">
        <form className="settings-card" onSubmit={e=>{e.preventDefault();void save({full_name:name,company},"Profile saved.")}}>
          <h2>Workspace profile</h2>
          <p className="settings-muted">Role: {settings.role}</p>
          <label>Email address<input value={settings.email} disabled/></label>
          <label>Full name<input value={name} minLength={2} maxLength={160} required onChange={e=>setName(e.target.value)}/></label>
          <label>Company name<input value={company} minLength={2} maxLength={160} required disabled={!(["owner","admin"].includes(settings.role))} onChange={e=>setCompany(e.target.value)}/></label>
          <button disabled={busy}>{busy?"Saving…":"Save profile"}</button>
        </form>
        <form className="settings-card" onSubmit={e=>{e.preventDefault();void save({current_password:currentPassword,new_password:newPassword},"Password changed. Other sessions were revoked.").then(ok=>{if(ok){setCurrentPassword("");setNewPassword("")}})}}>
          <h2>Security</h2>
          <p className="settings-muted">This session expires {new Date(settings.session_expires_at).toLocaleString()}.</p>
          <label>Current password<input type="password" autoComplete="current-password" required value={currentPassword} onChange={e=>setCurrentPassword(e.target.value)}/></label>
          <label>New password<input type="password" autoComplete="new-password" minLength={12} required value={newPassword} onChange={e=>setNewPassword(e.target.value)}/></label>
          <button disabled={busy}>{busy?"Saving…":"Change password"}</button>
        </form>
        {["owner","admin"].includes(settings.role)&&<section className="settings-card" aria-labelledby="zoho-title">
          <h2 id="zoho-title">Zoho Books live sync</h2>
          {zohoBanner&&<p role="status" className={["connected","sync"].includes(shownZohoFlag||"")?
            "settings-success":"settings-error"}>{zohoBanner}</p>}
          {zohoError&&<p role="alert" className="settings-error">{zohoError}</p>}
          {!zoho&&<p className="settings-muted">Loading…</p>}
          {zoho&&!zoho.configured&&
            <p className="settings-muted">Not set up on this server yet — see docs/ZOHO_SYNC.md.</p>}
          {zoho&&zoho.configured&&!zoho.connected&&<>
            <p className="settings-muted">Stream invoices from Zoho Books the moment they&apos;re created or
              updated, instead of uploading a file.</p>
            <button type="button" disabled={zohoBusy} onClick={()=>void connectZoho()}>
              {zohoBusy?"Connecting…":"Connect Zoho Books"}</button>
          </>}
          {zoho&&zoho.connected&&<>
            <p className="settings-muted">Connected{zoho.connected_at?` ${new Date(zoho.connected_at).toLocaleString()}`:""}
              {zoho.api_domain?` (${zoho.api_domain.replace("https://www.zohoapis","Zoho")} data centre)`:""}.{" "}
              {zoho.invoices_synced} invoice{zoho.invoices_synced===1?"":"s"} synced so far
              {zoho.last_synced_at?`, last ${new Date(zoho.last_synced_at).toLocaleString()}`:""}.</p>
            <label>Webhook URL — add this in Zoho Books under Settings → Automation → Webhooks
              <input readOnly value={zoho.webhook_url||""} onFocus={e=>e.target.select()}/></label>
            <p className="settings-muted">Webhooks only cover invoices created or edited from now on. To
              pull in invoices that already existed in Zoho before connecting, use Sync Now (safe to run
              again any time — it won&apos;t duplicate anything already imported).</p>
            <button type="button" disabled={zohoBusy} onClick={()=>void syncNow()}>
              {zohoBusy?"Working…":"Sync Now"}</button>
            {(zoho.events||[]).length>0&&<div>
              <p className="settings-muted" style={{marginBottom:6}}>Recent deliveries</p>
              <ul className="fu-mini">{(zoho.events||[]).slice(0,8).map((e,i)=><li key={i}>
                <strong>{e.invoice_number||"(no invoice number)"}</strong>
                <span>{e.outcome==="skipped"?"Skipped":e.outcome==="created"?"Imported":"Updated"}
                  {" · "}{new Date(e.received_at).toLocaleString()}</span>
                {e.reason&&<em>{e.reason}</em>}
              </li>)}</ul>
            </div>}
            <button type="button" disabled={zohoBusy} onClick={()=>void disconnectZoho()}>
              {zohoBusy?"Disconnecting…":"Disconnect"}</button>
          </>}
        </section>}
        {settings.role==="owner"&&<section className="settings-card settings-danger" aria-labelledby="erase-title">
          <h2 id="erase-title">Your data</h2>
          <p className="settings-muted">Delete everything imported into this workspace, or the whole account. Deletion is immediate and
            cannot be undone; backups expire within seven days. See the <Link href="/privacy">privacy page</Link>.</p>
          <label>Type the company name, {settings.company}, to confirm<input value={confirmName} onChange={e=>setConfirmName(e.target.value)} autoComplete="off"/></label>
          <button type="button" disabled={busy||confirmName!==settings.company} onClick={()=>void erase("data")}>Delete all workspace data</button>
          <label>Password, to delete the account<input type="password" autoComplete="current-password" value={erasePassword} onChange={e=>setErasePassword(e.target.value)}/></label>
          <button type="button" disabled={busy||confirmName!==settings.company||erasePassword.length<12} onClick={()=>void erase("account")}>Delete account and workspace</button>
        </section>}
      </div>}
    </main>
  </div>;
}
