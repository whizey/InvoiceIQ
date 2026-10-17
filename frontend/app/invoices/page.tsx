import {cookies} from "next/headers";
import {redirect} from "next/navigation";
import ReferenceFrame from "@/components/ReferenceFrame";
import PortalNavigation from "@/components/PortalNavigation";
import PortalOperations from "@/components/PortalOperations";
import {parsePortalFilters} from "@/lib/portal-filters";

export default async function Page({searchParams}:{searchParams:Promise<{q?:string;page?:string;sort?:string;view?:string}>}){
  const session=(await cookies()).get("invoiceiq_portal_session")?.value;
  const filters=await searchParams;
  if(session && filters.view==="analytics")return <div className="portal-shell"><PortalNavigation/><ReferenceFrame page="invoices" portal/></div>;
  if(session)return <PortalOperations section="invoices" initialFilters={parsePortalFilters(filters)}/>;
  if(process.env.INVOICEIQ_PORTAL_ENABLED==="true")redirect("/home");
  return <ReferenceFrame page="invoices"/>;
}
