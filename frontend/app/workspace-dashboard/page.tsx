import {cookies} from "next/headers";
import {redirect} from "next/navigation";
import ReferenceFrame from "@/components/ReferenceFrame";
import RecoveryPanel from "@/components/RecoveryPanel";
import PortalNavigation from "@/components/PortalNavigation";

export const metadata={title:"Analytics | InvoiceIQ"};

export default async function WorkspaceDashboard(){
  if(!(await cookies()).get("invoiceiq_portal_session")?.value)redirect("/home");
  return <div className="portal-shell">
    <PortalNavigation/>
    <ReferenceFrame page="overview" portal/>
    <a className="rp-jump" href="#recovery-pipeline">Recovery pipeline &amp; AI assistant ↓</a>
    <RecoveryPanel/>
  </div>;
}
