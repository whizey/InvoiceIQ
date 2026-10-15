"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";

export default function ConnectWorkspace() {
  const router = useRouter();
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token.trim() || busy) return;

    setBusy(true);
    setMessage("");

    try {
      const login = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ token: token.trim() }),
      });

      if (!login.ok) {
        throw new Error(`Session failed: HTTP ${login.status}`);
      }

      localStorage.setItem("invoiceiq-session-change", crypto.randomUUID());

      const [cases, finance] = await Promise.all([
        fetch("/api/workspace/cases", {
          credentials: "same-origin",
          cache: "no-store",
        }),
        fetch("/api/finance/overview", {
          credentials: "same-origin",
          cache: "no-store",
        }),
      ]);

      if (!cases.ok || !finance.ok) {
        await fetch("/api/session", {
          method: "DELETE",
          credentials: "same-origin",
        });
        localStorage.setItem("invoiceiq-session-change", crypto.randomUUID());
        throw new Error(
          `Cases HTTP ${cases.status}; Finance HTTP ${finance.status}`
        );
      }

      setToken("");
      setMessage("Connected. Loading dashboard...");
      router.push("/");
      router.refresh();
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : "Connection failed"
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={{
      minHeight: "100vh",
      background: "#090909",
      color: "#fff",
      display: "grid",
      placeItems: "center",
      padding: 24,
    }}>
      <form onSubmit={connect} style={{
        width: "100%",
        maxWidth: 420,
        display: "grid",
        gap: 20,
      }}>
        <h1 style={{ fontSize: 32, fontWeight: 700 }}>
          InvoiceIQ
        </h1>

        <p>Connect your workspace to load live financial data.</p>

        <input
          type="password"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          placeholder="Workspace credential"
          autoComplete="off"
          required
          style={{
            padding: 14,
            background: "#171717",
            border: "1px solid #555",
            borderRadius: 8,
            color: "white",
          }}
        />

        <button
          type="submit"
          disabled={busy}
          style={{
            padding: 14,
            background: "white",
            color: "black",
            borderRadius: 8,
            cursor: "pointer",
            fontWeight: 600,
          }}
        >
          {busy ? "Connecting..." : "Connect workspace"}
        </button>

        <p role="status">{message}</p>
      </form>
    </main>
  );
}
