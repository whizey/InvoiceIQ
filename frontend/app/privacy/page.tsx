import Link from "next/link";

export const metadata={title:"Privacy | InvoiceIQ"};

export default function PrivacyPage(){
  return <main className="pv">
    <p className="settings-eyebrow"><Link href="/home">InvoiceIQ</Link> / Privacy</p>
    <h1>Privacy</h1>
    <p className="pv-lead">What InvoiceIQ stores, who can see it, which services process it, and how to delete it.</p>

    <h2>What is stored</h2>
    <ul>
      <li><strong>Your account:</strong> name, email and a salted scrypt hash of your password (never the password itself).</li>
      <li><strong>Your workspace:</strong> the invoices and payments you import, notes, review decisions and the reminders you log as sent.</li>
      <li><strong>Sessions:</strong> a digest of your session token. Sessions end after eight hours or when you sign out.</li>
      <li><strong>Assistant records:</strong> which model answered, timings and whether figures checked out. The text of your questions and answers is not stored.</li>
    </ul>

    <h2>Who can see it</h2>
    <p>Only members of your workspace, with the role you give them (owner, admin or reviewer). Every request is checked against your
      workspace, and one company&apos;s records are never returned to another. Visitors to the public workspace see only their own notes,
      decisions and reminders.</p>

    <h2>Services that process it</h2>
    <ul>
      <li><strong>Hosting:</strong> the site runs on Vercel, the API on a Hugging Face Space, and the database is PostgreSQL hosted by Supabase,
        which encrypts stored data. Traffic between them is encrypted in transit (HTTPS/TLS).</li>
      <li><strong>AI models:</strong> when you ask the assistant a question or draft a reminder, the question and the relevant figures from your
        workspace are sent to Google Gemini or, as a backup, Groq, to write the answer. Nothing is sent when you don&apos;t use these features.</li>
      <li><strong>Tracing:</strong> answers may be traced in LangSmith to debug the assistant. For company workspaces, traces carry structure and
        timings only, not your invoice content.</li>
      <li><strong>Error tracking:</strong> if enabled, unexpected server errors are reported with the request path and error type, without request bodies.</li>
    </ul>
    <p>InvoiceIQ never sends messages to your customers and never moves money: you send reminders yourself.</p>

    <h2>Deleting your data</h2>
    <p>A workspace owner can delete all of the workspace&apos;s invoices, payments, notes, decisions and reminders, or delete the whole
      account and workspace, from <Link href="/settings">Settings</Link>. Deletion is immediate and cannot be undone. Database backups
      are kept for at most seven days, after which deleted data is gone from them too.</p>

  </main>;
}
