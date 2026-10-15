/** Resolve a server-only backend endpoint, including a private host:port. */
export function backendBase(): string {
  const configured = (process.env.INVOICEIQ_API_URL || process.env.API_BASE_URL || "http://127.0.0.1:8000").trim().replace(/\/$/, "");
  if (/^https?:\/\/[^\s/]+$/i.test(configured)) return configured;
  if (/^[a-z0-9.-]+:[0-9]+$/i.test(configured)) return `http://${configured}`;
  throw new Error("Invalid backend endpoint configuration");
}
