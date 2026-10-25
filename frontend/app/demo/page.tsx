import {cookies} from "next/headers";
import {redirect} from "next/navigation";
import DemoTimeline from "@/components/DemoTimeline";

export const metadata={title:"Watch it happen | InvoiceIQ"};

export default async function DemoPage(){
  if(!(await cookies()).get("invoiceiq_portal_session")?.value)redirect("/home");
  return <DemoTimeline/>;
}
