import {cookies} from "next/headers";
import {redirect} from "next/navigation";
import ReferenceFrame from "@/components/ReferenceFrame";
import PortalOperations from "@/components/PortalOperations";
import {parsePortalFilters} from "@/lib/portal-filters";

export default async function Page({searchParams}:{searchParams:Promise<{q?:string;page?:string;sort?:string}>}){
  const session=(await cookies()).get("invoiceiq_portal_session")?.value;
  if(session)return <PortalOperations section="cases" initialFilters={parsePortalFilters(await searchParams)}/>;
  if(process.env.INVOICEIQ_PORTAL_ENABLED==="true")redirect("/home");
  return <ReferenceFrame page="cases"/>;
}
