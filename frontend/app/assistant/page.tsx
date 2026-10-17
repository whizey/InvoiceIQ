import {cookies} from "next/headers";
import {redirect} from "next/navigation";
import PortalNavigation from "@/components/PortalNavigation";
import AssistantChat from "@/components/AssistantChat";

export const metadata={title:"AI assistant | InvoiceIQ"};

export default async function AssistantPage(){
  if(!(await cookies()).get("invoiceiq_portal_session")?.value)redirect("/home");
  return <div className="portal-shell">
    <PortalNavigation/>
    <main className="ai-page">
      <p className="settings-eyebrow">Workspace / AI assistant</p>
      <h1>AI assistant</h1>
      <p className="settings-intro">A receivables analyst grounded in your workspace data.</p>
      <AssistantChat/>
    </main>
  </div>;
}
