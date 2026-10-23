import {cookies} from "next/headers";
import {redirect} from "next/navigation";

/** Keep old bookmarks working, but skip the redundant showcase landing page. */
export default async function Page(){
  const hasPortalSession=Boolean((await cookies()).get("invoiceiq_portal_session")?.value);
  redirect(hasPortalSession?"/follow-ups":"/home");
}
