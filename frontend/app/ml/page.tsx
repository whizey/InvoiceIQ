import {redirect} from "next/navigation";

// The model page moved to /model, where the model is compared with the simple per-customer average.
export default function ModelIntelligenceRedirect(){
  redirect("/model");
}
