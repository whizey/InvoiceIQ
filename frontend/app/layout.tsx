import type { Metadata } from "next";
import Link from "next/link";
import { Geist } from "next/font/google";
import { LiveRefresh } from "@/components/LiveRefresh";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "AI Revenue Recovery",
  description:
    "AI-assisted B2B revenue recovery — detects overdue invoices, recommends recovery actions, and gates every action behind a deterministic policy engine.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} h-full antialiased`}>
      <body className="min-h-full bg-slate-50 font-sans text-slate-900">
        <header className="border-b border-slate-200 bg-white">
          <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
            <Link href="/" className="flex items-baseline gap-2">
              <span className="text-lg font-semibold tracking-tight text-slate-900">
                AI Revenue Recovery
              </span>
              <span className="hidden text-xs text-slate-400 sm:inline">
                portfolio demo · synthetic data
              </span>
            </Link>
            <LiveRefresh />
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
