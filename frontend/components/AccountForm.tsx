"use client";
import {useState} from "react";
import Link from "next/link";
import {useRouter} from "next/navigation";
export default function AccountForm({mode}:{mode:"register"|"login"}){
 const router=useRouter();const [error,setError]=useState("");const [loading,setLoading]=useState(false);
 async function submit(event:React.FormEvent<HTMLFormElement>){
  event.preventDefault();setError("");setLoading(true);
  const payload=Object.fromEntries(new FormData(event.currentTarget).entries());
  try{const r=await fetch("/api/portal/"+mode,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
   const data=await r.json();if(!r.ok)throw new Error(data.detail||"Unable to continue");
   router.push(mode==="register"?"/onboarding":"/follow-ups");router.refresh();
  }catch(e){setError(e instanceof Error?e.message:"Request failed")}finally{setLoading(false)}
 }
 const register=mode==="register";
 return <main style={{minHeight:"100dvh",display:"grid",placeItems:"center",background:"#f6f6f4",fontFamily:"Arial,sans-serif",color:"#171717",padding:24}}><div style={{background:"white",padding:"clamp(24px,5vw,50px)",border:"1px solid #e5e5e2",borderRadius:16,width:"100%",maxWidth:480}}>
 <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",gap:12}}><Link href="/home" style={{fontWeight:800,color:"inherit",textDecoration:"none"}}>▣ InvoiceIQ</Link><button type="button" onClick={()=>router.back()} style={{background:"white",border:"1px solid #ddd",borderRadius:6,padding:"8px 12px",cursor:"pointer"}}>← Back</button></div><h1 style={{fontSize:30,marginTop:32}}>{register?"Create your workspace":"Welcome back"}</h1><p style={{color:"#666"}}>{register?"Your company's records stay private to your workspace.":"Sign in to access your company workspace."}</p>
 <form onSubmit={submit} style={{display:"grid",gap:16,marginTop:28}}>
 {register&&<><label>Full name<input required minLength={2} maxLength={160} name="full_name" autoComplete="name" style={field}/></label><label>Company name<input required minLength={2} maxLength={160} name="company" style={field}/></label></>}
 <label>Email address<input required type="email" name="email" autoComplete="email" style={field}/></label>
 <label>Password<input required type="password" name="password" autoComplete={register?"new-password":"current-password"} minLength={register?12:1} style={field}/></label>
 {error&&<p role="alert" style={{color:"#b42318",margin:0}}>{error}</p>}
 <button disabled={loading} style={{background:"#141414",color:"white",border:0,borderRadius:8,padding:14,cursor:"pointer",fontSize:15}}>{loading?"Please wait…":register?"Create account":"Sign in"}</button></form>
 <p style={{color:"#666",fontSize:14,marginTop:25}}>{register?"Already have an account? ":"New to InvoiceIQ? "}<Link href={register?"/login":"/signup"}>{register?"Sign in":"Create one"}</Link></p>
 {register&&<p style={{color:"#666",fontSize:13}}>By creating a workspace you agree to how we handle data, described on the <Link href="/privacy">privacy page</Link>.</p>}
 </div></main>
}
const field:React.CSSProperties={display:"block",width:"100%",border:"1px solid #ddd",borderRadius:7,padding:12,marginTop:7,fontSize:15};
