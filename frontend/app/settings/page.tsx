import {cookies} from "next/headers";
import {redirect} from "next/navigation";
import AccountSettings from "@/components/AccountSettings";

export const metadata={title:"Settings | InvoiceIQ"};

export default async function SettingsPage(){
  if(!(await cookies()).get("invoiceiq_portal_session")?.value)redirect("/home");
  return <AccountSettings/>;
}
