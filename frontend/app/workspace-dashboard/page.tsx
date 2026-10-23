import {cookies} from "next/headers";
import {redirect} from "next/navigation";
import ReferenceFrame from "@/components/ReferenceFrame";
import RecoveryCharts from "@/components/RecoveryCharts";
import PortalNavigation from "@/components/PortalNavigation";
import Term from "@/components/Term";

export const metadata={title:"Analytics | InvoiceIQ"};

export default async function WorkspaceDashboard(){
  if(!(await cookies()).get("invoiceiq_portal_session")?.value)redirect("/home");
  return <div className="portal-shell">
    <PortalNavigation/>
    <p className="page-intro dashboard-intro">What customers owe this business, how much of it is <Term k="overdue">overdue</Term>, how late it is (<Term k="ageing">ageing</Term>) and what came in. Unpaid invoices carry a <Term k="risk">late-payment risk</Term> score.</p>
    <ReferenceFrame page="overview" portal/>
    <RecoveryCharts view="dashboard"/>
  </div>;
}
