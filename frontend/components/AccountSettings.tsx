"use client";

import {useCallback,useEffect,useState} from "react";
import {useRouter} from "next/navigation";
import Link from "next/link";
import PortalNavigation from "@/components/PortalNavigation";

type Settings={email:string;full_name:string;company:string;role:string;session_expires_at:string};

export default function AccountSettings(){
  const router=useRouter();
  const [settings,setSettings]=useState<Settings|null>(null);
  const [visitor,setVisitor]=useState(false);
  const [name,setName]=useState("");
  const [company,setCompany]=useState("");
  const [currentPassword,setCurrentPassword]=useState("");
  const [newPassword,setNewPassword]=useState("");
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [success,setSuccess]=useState("");
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
      </div>}
    </main>
  </div>;
}
