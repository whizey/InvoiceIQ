import { notFound, redirect } from "next/navigation";

const uuid = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i;

export default async function CaseLink({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  if (!uuid.test(id)) notFound();
  redirect(`/cases?case_id=${encodeURIComponent(id)}`);
}
