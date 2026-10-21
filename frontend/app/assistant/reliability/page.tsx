import {cookies} from "next/headers";
import Link from "next/link";
import {redirect} from "next/navigation";
import PortalNavigation from "@/components/PortalNavigation";
import AssistantReliability from "@/components/AssistantReliability";

export const metadata={title:"Assistant reliability | InvoiceIQ"};

export default async function AssistantReliabilityPage(){
  if(!(await cookies()).get("invoiceiq_portal_session")?.value)redirect("/home");
  return <div className="portal-shell">
    <PortalNavigation/>
    <main className="ai-page">
      <p className="settings-eyebrow">Workspace / <Link href="/assistant">AI assistant</Link> / Reliability</p>
      <h1>Assistant reliability</h1>
      <p className="settings-intro">How the assistant has been answering: which model answered, whether its figures matched your records, how long it took, and how accurate it is on a fixed test set.</p>
      <AssistantReliability/>
    </main>
  </div>;
}
