import {redirect} from "next/navigation";

// The standalone model-intelligence page was retired; scoring remains available through the API.
export default function ModelIntelligenceRedirect(){
  redirect("/workspace-dashboard");
}
