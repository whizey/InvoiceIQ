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
 const token=(await cookies()).get('invoiceiq_session')?.value;
 if(!token&&process.env.INVOICEIQ_DEMO_SYNTHETIC!=='true')return Response.json({detail:'Workspace authentication required'},{status:401});
 try{
  const upstream=await fetch(`${backendBase()}${target}${request.nextUrl.search}`,{headers:token?{Authorization:`Bearer ${token}`}:{},cache:'no-store',signal:AbortSignal.any([request.signal,AbortSignal.timeout(30000)])});
  return new Response(upstream.body,{status:upstream.status,headers:{'Content-Type':upstream.headers.get('content-type')||'application/json','Cache-Control':'no-store',...(upstream.headers.get('content-disposition')?{'Content-Disposition':upstream.headers.get('content-disposition')!}:{})}});
 }catch{return Response.json({detail:'Backend unavailable. Check INVOICEIQ_API_URL and the FastAPI service.'},{status:503});}
}
