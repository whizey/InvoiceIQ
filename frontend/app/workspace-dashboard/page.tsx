import {cookies} from "next/headers";
import {redirect} from "next/navigation";
import ReferenceFrame from "@/components/ReferenceFrame";
export const metadata={title:"Analytics | InvoiceIQ"};
export default async function WorkspaceDashboard(){
  if(!(await cookies()).get("invoiceiq_portal_session")?.value)redirect("/platform");
  return <ReferenceFrame page="overview"/>;
}
