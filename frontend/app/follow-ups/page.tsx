import {cookies} from "next/headers";
import {redirect} from "next/navigation";
import FollowUps from "@/components/FollowUps";

export const metadata={title:"Follow-ups | InvoiceIQ"};

export default async function FollowUpsPage(){
  if(!(await cookies()).get("invoiceiq_portal_session")?.value)redirect("/home");
  return <FollowUps/>;
}
