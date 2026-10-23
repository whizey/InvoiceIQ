import {NextRequest} from "next/server";
import {cookies} from "next/headers";
import {backendBase} from "../../../../lib/backend-base";
export const dynamic = "force-dynamic";
const allowedGet = new Set(["me", "overview", "invoices", "settings", "intelligence", "recovery-summary","alerts","assistant-traces","assistant-reliability","notes","follow-ups","example-export","reminders","zoho-status"]);
const allowedPost = new Set(["register", "login", "showcase", "decisions", "import", "score", "assistant", "assistant-stream", "transcribe", "notes", "import-preview", "import-file", "reminder-draft", "reminders", "erase", "zoho-connect", "zoho-disconnect"]);
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
  if ((allowedGet.has(action)||["decisions","score","session","assistant","assistant-stream","transcribe","notes","import-preview","import-file","reminder-draft","reminders","erase","zoho-connect","zoho-disconnect"].includes(action)) && !token)
    return Response.json({detail:"Sign in required"},{status:401});
  const body=(method==="POST" && action!=="showcase")||method==="PATCH" ? await req.text():undefined;
  if (body && body.length>(action==="import"?262144:action==="import-preview"||action==="import-file"?3500000:action==="assistant"||action==="assistant-stream"?16384:action==="transcribe"?1000000:action==="reminders"?8192:4096)) return Response.json({detail:"Payload too large"},{status:413});
  try {
    const query=["invoices","assistant-reliability","notes","example-export","showcase"].includes(action)?req.nextUrl.search:"";
    const upstream=await fetch(backendBase()+"/portal/"+path.map(encodeURIComponent).join("/")+query,{
      method,cache:"no-store",signal:AbortSignal.timeout(action==="assistant"||action==="assistant-stream"||action==="import-file"||action==="reminder-draft"?60000:action==="transcribe"?60000:15000),
      headers:{...(token?{Authorization:"Bearer "+token}:{}),...(body?{"Content-Type":"application/json"}:{})},
      body,
    });
    // Progress events pass straight through; the browser reads them as they arrive.
    if (action==="assistant-stream" && upstream.ok && upstream.body)
      return new Response(upstream.body,{headers:{"Content-Type":"text/event-stream","Cache-Control":"no-cache, no-transform",
        "X-Accel-Buffering":"no"}});
    const data=await upstream.json().catch(()=>({detail:"Invalid upstream response"}));
    if (upstream.ok && ["register","login","showcase"].includes(action)) {
      if (typeof data.token!=="string" || !data.token.startsWith("iqp_"))
        return Response.json({detail:"Invalid session"},{status:503});
      // Lax, not Strict: the Zoho OAuth callback redirects the browser back here as a top-level GET that
      // originated by leaving this site, and a Strict cookie is dropped on that return trip. Every mutating
      // request is still checked against its Origin header above, so Lax loses no real CSRF protection here.
      (await cookies()).set(cookieName,data.token,{httpOnly:true,secure:req.nextUrl.protocol==="https:",
        sameSite:"lax",path:"/",maxAge:8*3600});
      return Response.json({ok:true,kind:data.kind,workspace:data.workspace},{headers:{"Cache-Control":"no-store"}});
    }
    if ((method==="DELETE" && (upstream.ok||upstream.status===401)) || (action==="erase" && upstream.ok && data.scope==="account")) {
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
