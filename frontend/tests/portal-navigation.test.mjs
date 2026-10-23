import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
const read=(p)=>fs.readFileSync(new URL(p,import.meta.url),"utf8");

test("workspace entry opens the follow-up list directly",()=>{
  const home=read("../app/home/page.tsx");
  const entry=read("../components/ExploreShowcaseButton.tsx");
  const redirect=read("../app/platform/page.tsx");
  const auth=read("../components/AccountForm.tsx");
  assert.ok(home.includes("ExploreShowcaseButton"));
  assert.ok(!home.includes('href="/platform"'));
  assert.ok(entry.includes("/api/portal/showcase"));
  assert.ok(entry.includes("/api/portal/me"));
  assert.ok(entry.includes('router.push("/follow-ups")'));
  assert.ok(redirect.includes('redirect(hasPortalSession?"/follow-ups":"/home")'));
  assert.ok(auth.includes('router.push("/follow-ups")'));
});
test("browser history back control works across SaaS routes",()=>{
  const nav=read("../components/PortalNavigation.tsx");
  const frame=read("../components/ReferenceFrame.tsx");
  assert.ok(nav.includes("router.back()"));
  assert.ok(nav.includes('aria-label="Go back to previous page"'));
  assert.ok(frame.includes('doc.addEventListener("click",navigate,true)'));
  assert.ok(frame.includes("router.push((destination.pathname"));
  const dash=read("../app/workspace-dashboard/page.tsx");
  assert.ok(dash.includes("<PortalNavigation/>"));
  assert.ok(dash.includes('<ReferenceFrame page="overview" portal/>'));
  for(const section of ["invoices","cases","approvals"]){
    const route=read(`../app/${section}/page.tsx`);
    assert.ok(route.includes(`<PortalOperations section="${section}" initialFilters={parsePortalFilters(`));
    assert.ok(route.includes(`<ReferenceFrame page="${section}"/>`));
  }
  assert.ok(read("../app/invoices/page.tsx").includes('<ReferenceFrame page="invoices" portal/>'));
});
test("retained KPI template keeps its plain-language labels",()=>{
  const s=read("../public/reference/dashboard-data.js");
  for(const label of ["Total billed","Still owed to you","Overdue","Share that is late","Payments received","Risk scores"]){
    assert.ok(s.includes(label));
  }
});
