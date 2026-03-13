'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useAuth } from '@/providers/AuthProvider';

interface SidebarProps {
  /** Called when any navigation link is clicked (used by mobile menu to close). */
  onLinkClick?: () => void;
}

export function Sidebar({ onLinkClick }: SidebarProps) {
  const { user, loading } = useAuth();
  const pathname = usePathname();
  const isAdmin = !loading && user?.role === 'admin';

  return (
    <nav className="flex flex-col gap-1 p-4 text-sm">
      <p className="mb-1 px-2 text-xs font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500">
        Explore
      </p>
      <SidebarLink href="/search" active={pathname === '/search'} onClick={onLinkClick}>
        Search Rulings
      </SidebarLink>
      <SidebarLink href="/rulings" active={pathname === '/rulings'} onClick={onLinkClick}>
        Latest Rulings
      </SidebarLink>

      <p className="mb-1 mt-4 px-2 text-xs font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500">
        Research
      </p>
      <SidebarLink href="/cases" active={pathname === '/cases'} onClick={onLinkClick}>
        Cases
      </SidebarLink>
      <SidebarLink href="/judges" active={pathname === '/judges'} onClick={onLinkClick}>
        Judges
      </SidebarLink>

      {isAdmin && (
        <>
          <p className="mb-1 mt-4 border-t border-slate-200 pt-4 px-2 text-xs font-semibold uppercase tracking-wider text-slate-400 dark:border-slate-700 dark:text-slate-500">
            Admin
          </p>
          <SidebarLink
            href="/admin/data-quality/"
            active={pathname.startsWith('/admin/data-quality')}
            onClick={onLinkClick}
          >
            Data Health
          </SidebarLink>
        </>
      )}
    </nav>
  );
}

/** Wraps the Sidebar in the desktop aside container. Used by the root layout. */
export function DesktopSidebar() {
  return (
    <aside className="sticky top-14 hidden h-[calc(100vh-3.5rem)] w-56 shrink-0 border-r border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900 lg:block">
      <Sidebar />
    </aside>
  );
}

function SidebarLink({
  href,
  active,
  onClick,
  children,
}: {
  href: string;
  active: boolean;
  onClick?: () => void;
  children: React.ReactNode;
}) {
  const baseClasses =
    'rounded-md px-2 py-1.5 hover:bg-slate-200 hover:text-slate-900 dark:hover:bg-slate-800 dark:hover:text-slate-100';
  const activeClasses = active
    ? 'bg-slate-200 text-slate-900 dark:bg-slate-800 dark:text-slate-100'
    : 'text-slate-700 dark:text-slate-300';

  return (
    <Link href={href} className={`${baseClasses} ${activeClasses}`} onClick={onClick}>
      {children}
    </Link>
  );
}
