import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
const read=(p)=>fs.readFileSync(new URL(p,import.meta.url),"utf8");

test("showcase entry opens analytics directly without intermediate workspace page",()=>{
  const home=read("../app/home/page.tsx");
  const entry=read("../components/ExploreShowcaseButton.tsx");
  const redirect=read("../app/platform/page.tsx");
  const auth=read("../components/AccountForm.tsx");
  assert.ok(home.includes("ExploreShowcaseButton"));
  assert.ok(!home.includes('href="/platform"'));
  assert.ok(entry.includes("/api/portal/showcase"));
  assert.ok(entry.includes("/api/portal/me"));
  assert.ok(entry.includes('router.push("/workspace-dashboard")'));
  assert.ok(redirect.includes('redirect(hasPortalSession?"/workspace-dashboard":"/home")'));
  assert.ok(auth.includes('router.push("/workspace-dashboard")'));
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
    assert.ok(route.includes(`<PortalOperations section="${section}"/>`));
    assert.ok(route.includes(`<ReferenceFrame page="${section}"/>`));
  }
});
test("retained KPI template and source adapter are not redesigned",()=>{
  const s=read("../public/reference/dashboard-data.js");
  for(const label of ["Invoiced","Open receivables","Overdue exposure","Overdue share","Collected","Score coverage"]){
    assert.ok(s.includes(label));
  }
});
