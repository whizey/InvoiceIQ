import Link from "next/link";

export default function NotFound() {
  return (
    <div className="rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center">
      <p className="text-lg font-semibold text-slate-900">Not found</p>
      <p className="mt-1 text-sm text-slate-500">
        That page or recovery case doesn&apos;t exist.
      </p>
      <Link href="/" className="mt-4 inline-block text-sm text-indigo-600 hover:underline">
        ← Back to dashboard
      </Link>
    </div>
  );
}
