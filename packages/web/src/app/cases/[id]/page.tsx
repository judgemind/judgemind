type Props = { params: Promise<{ id: string }> };

export default async function CaseDetailPage({ params }: Props) {
  const { id } = await params;

  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">Case {id}</h1>
      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">Case details coming soon.</p>
    </div>
  );
}
