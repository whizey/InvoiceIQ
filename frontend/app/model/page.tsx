import {cookies} from "next/headers";
import {redirect} from "next/navigation";
import ModelEvidence from "@/components/ModelEvidence";

export const metadata={title:"Model | InvoiceIQ"};

export default async function ModelPage(){
  if(!(await cookies()).get("invoiceiq_portal_session")?.value)redirect("/home");
  return <ModelEvidence/>;
}
