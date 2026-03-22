'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Search, FileText, FolderOpen, Gavel, BarChart3 } from 'lucide-react';
import { useAuth } from '@/providers/AuthProvider';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { cn } from '@/lib/utils';

interface SidebarProps {
  /** Called when any navigation link is clicked (used by mobile menu to close). */
  onLinkClick?: () => void;
}

export function Sidebar({ onLinkClick }: SidebarProps) {
  const { user, loading } = useAuth();
  const pathname = usePathname();
  const isAdmin = !loading && user?.role === 'admin';

  return (
    <nav aria-label="Sidebar" className="flex flex-col gap-1 p-4 text-sm">
      <h2 className="mb-1 px-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Explore
      </h2>
      <SidebarLink
        href="/search"
        icon={<Search className="h-4 w-4" aria-hidden="true" />}
        active={pathname === '/search'}
        onClick={onLinkClick}
      >
        Search Rulings
      </SidebarLink>
      <SidebarLink
        href="/rulings"
        icon={<FileText className="h-4 w-4" aria-hidden="true" />}
        active={pathname === '/rulings'}
        onClick={onLinkClick}
      >
        Latest Rulings
      </SidebarLink>

      <Separator className="my-3" />

      <h2 className="mb-1 px-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Research
      </h2>
      <SidebarLink
        href="/cases"
        icon={<FolderOpen className="h-4 w-4" aria-hidden="true" />}
        active={pathname === '/cases'}
        onClick={onLinkClick}
      >
        Cases
      </SidebarLink>
      <SidebarLink
        href="/judges"
        icon={<Gavel className="h-4 w-4" aria-hidden="true" />}
        active={pathname === '/judges'}
        onClick={onLinkClick}
      >
        Judges
      </SidebarLink>

      {isAdmin && (
        <>
          <Separator className="my-3" />
          <h2 className="mb-1 px-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Admin
          </h2>
          <SidebarLink
            href="/admin/data-quality/"
            icon={<BarChart3 className="h-4 w-4" aria-hidden="true" />}
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
    <aside aria-label="Sidebar navigation" className="sticky top-14 hidden h-[calc(100vh-3.5rem)] w-56 shrink-0 border-r bg-muted/40 lg:block">
      <Sidebar />
    </aside>
  );
}

function SidebarLink({
  href,
  icon,
  active,
  onClick,
  children,
}: {
  href: string;
  icon: React.ReactNode;
  active: boolean;
  onClick?: () => void;
  children: React.ReactNode;
}) {
  return (
    <Button
      variant={active ? 'secondary' : 'ghost'}
      size="sm"
      className={cn(
        'w-full justify-start gap-2',
        active && 'bg-accent text-accent-foreground font-medium',
      )}
      asChild
    >
      <Link href={href} onClick={onClick}>
        {icon}
        {children}
      </Link>
    </Button>
  );
}
