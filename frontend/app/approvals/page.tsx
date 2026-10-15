import {cookies} from "next/headers";
import {redirect} from "next/navigation";
import ReferenceFrame from "@/components/ReferenceFrame";
import PortalOperations from "@/components/PortalOperations";

export default async function Page(){
  const session=(await cookies()).get("invoiceiq_portal_session")?.value;
  if(session)return <PortalOperations section="approvals"/>;
  if(process.env.INVOICEIQ_PORTAL_ENABLED==="true")redirect("/home");
  return <ReferenceFrame page="approvals"/>;
}
