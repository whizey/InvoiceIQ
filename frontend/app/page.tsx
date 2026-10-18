import {redirect} from "next/navigation";
import ReferenceFrame from "@/components/ReferenceFrame";

export default function Page() {
  // Keep the original dashboard unchanged for existing deployments.
  // Public SaaS visitors see the professional product site when the portal
  // has explicitly been enabled in the frontend environment.
  if (process.env.INVOICEIQ_PORTAL_ENABLED === "true") redirect("/home");
  return <ReferenceFrame page="overview" />;
}
