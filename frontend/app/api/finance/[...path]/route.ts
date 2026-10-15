import { cookies } from 'next/headers';
import { NextRequest } from 'next/server';
import { backendBase } from '../../../../lib/backend-base';
export const dynamic='force-dynamic';
const uuid='[0-9a-fA-F-]{36}';
export async function GET(request:NextRequest,{params}:{params:Promise<{path:string[]}>}){
 const path=(await params).path.join('/');
 const routes:Record<string,string>={overview:'/dashboard/preserved',export:'/dashboard/preserved/export',calendar:'/dashboard/calendar',workflow:'/dashboard/analytics'};
 const target=routes[path] ?? (new RegExp(`^invoices/${uuid}/evidence$`).test(path)||new RegExp(`^recovery-cases/${uuid}$`).test(path)?`/${path}`:null);
 if(!target)return Response.json({detail:'Unknown resource'},{status:404});
 const jar=await cookies();
 const portal=jar.get('invoiceiq_portal_session')?.value;
 if(portal){
   if(path!=='overview')return Response.json({detail:'This operation is unavailable in the isolated portal'}, {status:403});
   try{
     const upstream=await fetch(backendBase()+'/portal/dashboard'+request.nextUrl.search,{headers:{Authorization:'Bearer '+portal},cache:'no-store',signal:AbortSignal.timeout(30000)});
     return new Response(upstream.body,{status:upstream.status,headers:{'Content-Type':'application/json','Cache-Control':'no-store'}});
   }catch{return Response.json({detail:'Portal financial API unavailable'},{status:503});}
 }
 const token=jar.get('invoiceiq_session')?.value;
 // Public reads are authorized and restricted by Render PUBLIC_READ_ONLY mode; never expose write endpoints.
 try{
  const upstream=await fetch(`${backendBase()}${target}${request.nextUrl.search}`,{headers:token?{Authorization:`Bearer ${token}`}:{},cache:'no-store',signal:AbortSignal.any([request.signal,AbortSignal.timeout(30000)])});
  return new Response(upstream.body,{status:upstream.status,headers:{'Content-Type':upstream.headers.get('content-type')||'application/json','Cache-Control':'no-store',...(upstream.headers.get('content-disposition')?{'Content-Disposition':upstream.headers.get('content-disposition')!}:{})}});
 }catch{return Response.json({detail:'Backend unavailable. Check INVOICEIQ_API_URL and the FastAPI service.'},{status:503});}
}
