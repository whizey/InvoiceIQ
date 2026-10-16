import {NextRequest} from "next/server";
import {cookies} from "next/headers";
import {backendBase} from "../../../../lib/backend-base";
export const dynamic = "force-dynamic";
const allowedGet = new Set(["me", "overview", "invoices", "settings", "intelligence", "recovery-summary"]);
const allowedPost = new Set(["register", "login", "showcase", "decisions", "import", "score", "assistant", "transcribe"]);
const cookieName = "invoiceiq_portal_session";
type Params = {params: Promise<{path: string[]}>};
async function handler(req: NextRequest, context: Params, method: "GET"|"POST"|"PATCH"|"DELETE") {
  const path = (await context.params).path;
  if (path.length !== 1 && !(method==="GET" && path.length===2 && path[0]==="intelligence"))
    return Response.json({detail:"Not found"},{status:404});
  const action = path[0];
  if (method==="GET" && !allowedGet.has(action)) return Response.json({detail:"Not found"},{status:404});
  if (method==="POST" && !allowedPost.has(action)) return Response.json({detail:"Not found"},{status:404});
  if (method==="PATCH" && action!=="settings") return Response.json({detail:"Not found"},{status:404});
  if (method==="DELETE" && action!=="session") return Response.json({detail:"Not found"},{status:404});
  const host=req.headers.get("host");
  const protocol=(req.headers.get("x-forwarded-proto")||req.nextUrl.protocol.slice(0,-1)).split(",")[0].trim();
  const expectedOrigin=host?`${protocol}://${host}`:"";
  if (method!=="GET" && (!expectedOrigin||req.headers.get("origin")!==expectedOrigin))
    return Response.json({detail:"Origin not allowed"},{status:403});
  const token=(await cookies()).get(cookieName)?.value;
  if ((allowedGet.has(action)||["decisions","score","session","assistant","transcribe"].includes(action)) && !token)
    return Response.json({detail:"Sign in required"},{status:401});
  const body=(method==="POST" && action!=="showcase")||method==="PATCH" ? await req.text():undefined;
  if (body && body.length>(action==="import"?262144:action==="assistant"?16384:action==="transcribe"?1000000:4096)) return Response.json({detail:"Payload too large"},{status:413});
  try {
    const query=action==="invoices"?req.nextUrl.search:"";
    const upstream=await fetch(backendBase()+"/portal/"+path.map(encodeURIComponent).join("/")+query,{
      method,cache:"no-store",signal:AbortSignal.timeout(action==="assistant"?30000:action==="transcribe"?60000:15000),
      headers:{...(token?{Authorization:"Bearer "+token}:{}),...(body?{"Content-Type":"application/json"}:{})},
      body,
    });
    const data=await upstream.json().catch(()=>({detail:"Invalid upstream response"}));
    if (upstream.ok && ["register","login","showcase"].includes(action)) {
      if (typeof data.token!=="string" || !data.token.startsWith("iqp_"))
        return Response.json({detail:"Invalid session"},{status:503});
      (await cookies()).set(cookieName,data.token,{httpOnly:true,secure:req.nextUrl.protocol==="https:",
        sameSite:"strict",path:"/",maxAge:8*3600});
      return Response.json({ok:true,kind:data.kind,workspace:data.workspace},{headers:{"Cache-Control":"no-store"}});
    }
    if (method==="DELETE" && (upstream.ok||upstream.status===401)) {
      (await cookies()).delete(cookieName);
      return Response.json({ok:true},{headers:{"Cache-Control":"no-store"}});
    }
    return Response.json(data,{status:upstream.status,headers:{"Cache-Control":"no-store"}});
  } catch {
    return Response.json({detail:"InvoiceIQ portal API unavailable"},{status:503});
  }
}
export function GET(req:NextRequest,ctx:Params){return handler(req,ctx,"GET")}
export function POST(req:NextRequest,ctx:Params){return handler(req,ctx,"POST")}
export function PATCH(req:NextRequest,ctx:Params){return handler(req,ctx,"PATCH")}
export function DELETE(req:NextRequest,ctx:Params){return handler(req,ctx,"DELETE")}
