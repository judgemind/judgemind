import Link from 'next/link';

export function Sidebar() {
  return (
    <aside className="sticky top-14 hidden h-[calc(100vh-3.5rem)] w-56 shrink-0 border-r border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900 lg:block">
      <nav className="flex flex-col gap-1 p-4 text-sm">
        <p className="mb-1 px-2 text-xs font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500">
          Explore
        </p>
        <SidebarLink href="/search">Search Rulings</SidebarLink>
        <SidebarLink href="/rulings">Latest Rulings</SidebarLink>

        <p className="mb-1 mt-4 px-2 text-xs font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500">
          Research
        </p>
        <SidebarLink href="/cases">Cases</SidebarLink>
        <SidebarLink href="/judges">Judges</SidebarLink>
      </nav>
    </aside>
  );
}

function SidebarLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link
      href={href}
      className="rounded-md px-2 py-1.5 text-slate-700 hover:bg-slate-200 hover:text-slate-900 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-slate-100"
    >
      {children}
    </Link>
  );
}
