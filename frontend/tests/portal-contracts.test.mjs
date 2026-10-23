import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const read=(path)=>fs.readFileSync(new URL(path,import.meta.url),"utf8");

test("financial dashboard KPI adapter keeps its labels and stays independent",()=>{
 const dashboard=read("../public/reference/dashboard-data.js");
 assert.ok(dashboard.includes("Total billed"));
 assert.ok(dashboard.includes("Risk scores"));
 assert.ok(!dashboard.includes("PortalWorkspace"));
});
test("portal proxy requires same-origin mutation and secure cookie flags",()=>{
 const route=read("../app/api/portal/[...path]/route.ts");
 assert.ok(route.includes('req.headers.get("origin")!==expectedOrigin'));
 assert.ok(route.includes('req.headers.get("host")'));
 assert.ok(route.includes("httpOnly:true"));
 assert.ok(route.includes('sameSite:"strict"'));
 assert.ok(route.includes('maxAge:8*3600'));
 assert.ok(!route.includes("NEXT_PUBLIC_"));
});
test("open-workspace action and account surfaces are separate",()=>{
 const entry=read("../components/ExploreShowcaseButton.tsx");
 const operations=read("../components/PortalOperations.tsx");
 const auth=read("../components/AccountForm.tsx");
 assert.ok(entry.includes("/api/portal/showcase"));
 assert.ok(operations.includes("/api/portal/decisions"));
 assert.ok(auth.includes("/api/portal/"));
 assert.ok(!auth.includes("/api/portal/showcase"));
});

test("portal export is session-scoped and retains existing dashboard export",()=>{
 const route=read("../app/api/finance/[...path]/route.ts");
 assert.ok(route.includes("'/portal/export'"));
 assert.ok(route.includes("'Content-Disposition'"));
 assert.ok(route.includes("path!=='overview'&&path!=='export'"));
});
