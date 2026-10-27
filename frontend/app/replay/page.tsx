import {cookies} from "next/headers";
import {redirect} from "next/navigation";
import ReplayTimeline from "@/components/ReplayTimeline";

export const metadata={title:"Watch it happen | InvoiceIQ"};

export default async function ReplayPage(){
  if(!(await cookies()).get("invoiceiq_portal_session")?.value)redirect("/home");
  return <ReplayTimeline/>;
}
