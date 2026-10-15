import {NextRequest} from "next/server";
import {cookies} from "next/headers";
import {backendBase} from "../../../../lib/backend-base";
export const dynamic = "force-dynamic";
const allowedGet = new Set(["me", "overview"]);
const allowedPost = new Set(["register", "login", "showcase", "decisions", "import"]);
const cookieName = "invoiceiq_portal_session";
type Params = {params: Promise<{path: string[]}>};
async function handler(req: NextRequest, context: Params, method: "GET"|"POST"|"DELETE") {
  const path = (await context.params).path;
  if (path.length !== 1) return Response.json({detail:"Not found"},{status:404});
  const action = path[0];
  if (method==="GET" && !allowedGet.has(action)) return Response.json({detail:"Not found"},{status:404});
  if (method==="POST" && !allowedPost.has(action)) return Response.json({detail:"Not found"},{status:404});
  if (method==="DELETE" && action!=="session") return Response.json({detail:"Not found"},{status:404});
  if (method!=="GET" && req.headers.get("origin")!==req.nextUrl.origin)
    return Response.json({detail:"Origin not allowed"},{status:403});
  const token=(await cookies()).get(cookieName)?.value;
  if ((allowedGet.has(action)||action==="decisions"||action==="session") && !token)
    return Response.json({detail:"Sign in required"},{status:401});
  const body=method==="POST" && action!=="showcase" ? await req.text():undefined;
  if (body && body.length>(action==="import"?262144:4096)) return Response.json({detail:"Payload too large"},{status:413});
  try {
    const upstream=await fetch(backendBase()+"/portal/"+action,{
      method,cache:"no-store",signal:AbortSignal.timeout(15000),
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
    if (method==="DELETE" && upstream.ok) (await cookies()).delete(cookieName);
    return Response.json(data,{status:upstream.status,headers:{"Cache-Control":"no-store"}});
  } catch {
    return Response.json({detail:"InvoiceIQ portal API unavailable"},{status:503});
  }
}
export function GET(req:NextRequest,ctx:Params){return handler(req,ctx,"GET")}
export function POST(req:NextRequest,ctx:Params){return handler(req,ctx,"POST")}
export function DELETE(req:NextRequest,ctx:Params){return handler(req,ctx,"DELETE")}
