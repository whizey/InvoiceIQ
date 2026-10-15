import {cookies} from 'next/headers';
import {NextRequest} from 'next/server';
import {backendBase} from '../../../lib/backend-base';

const cookieName = 'invoiceiq_session';
function sameOrigin(request:NextRequest){return request.headers.get('origin')===request.nextUrl.origin;}

export async function POST(request:NextRequest){
 if(!sameOrigin(request))return Response.json({detail:'Cross-origin credential changes are blocked'},{status:403});
 const body=await request.json().catch(()=>null);
 if(typeof body?.token!=='string'||!body.token.trim()||body.token.length>2048||/[\r\n]/.test(body.token))return Response.json({detail:'Enter a valid workspace credential'},{status:422});
 let upstream:Response;
 try{
  upstream=await fetch(backendBase()+'/auth/sessions',{method:'POST',headers:{Authorization:`Bearer ${body.token.trim()}`},cache:'no-store',signal:AbortSignal.timeout(10000)});
 }catch{return Response.json({detail:'Authentication service unavailable'},{status:503});}
 if(!upstream.ok)return Response.json({detail:'Invalid workspace credentials'},{status:upstream.status===429?429:upstream.status===503?503:401});
 const session=await upstream.json().catch(()=>null);
 const remaining=Math.floor((Date.parse(session?.expires_at)-Date.now())/1000);
 if(typeof session?.token!=='string'||!session.token.startsWith('iqs_')||!Number.isFinite(remaining)||remaining<1||remaining>8*60*60+60)return Response.json({detail:'Authentication service returned an invalid session'},{status:503});
 (await cookies()).set(cookieName,session.token,{httpOnly:true,sameSite:'strict',secure:request.nextUrl.protocol==='https:',path:'/',maxAge:remaining});
 return Response.json({ok:true,role:'viewer'},{headers:{'Cache-Control':'no-store'}});
}

export async function DELETE(request:NextRequest){
 if(!sameOrigin(request))return Response.json({detail:'Cross-origin credential changes are blocked'},{status:403});
 const jar=await cookies();
 const token=jar.get(cookieName)?.value;
 if(!token)return Response.json({ok:true});
 let status:number;
 try{
  const upstream=await fetch(backendBase()+'/auth/sessions/current',{method:'DELETE',headers:{Authorization:`Bearer ${token}`},cache:'no-store',signal:AbortSignal.timeout(10000)});
  status=upstream.status;
 }catch{return Response.json({detail:'Logout service unavailable; retry logout'},{status:503});}
 if(status!==200&&status!==401)return Response.json({detail:'Logout service unavailable; retry logout'},{status:503});
 jar.delete(cookieName);
 return Response.json({ok:true},{headers:{'Cache-Control':'no-store'}});
}
