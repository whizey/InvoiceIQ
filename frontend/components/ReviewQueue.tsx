"use client";

import Workspace from "./WorkspaceLive";

/** Compatibility entry point for the current approvals workspace. */
export function ReviewQueue() {
  return <Workspace type="approvals" />;
}
