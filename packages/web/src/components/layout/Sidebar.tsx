'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Search, FileText, FolderOpen, Gavel, BarChart3 } from 'lucide-react';
import { useAuth } from '@/providers/AuthProvider';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';

/** Navigation item definition shared by all sidebar variants. */
interface NavItem {
  href: string;
  icon: React.ReactNode;
  label: string;
  activeFn: (pathname: string) => boolean;
  adminOnly?: boolean;
}

/** Central list of nav items to keep all sidebar variants in sync. */
function useNavItems(): NavItem[] {
  const { user, loading } = useAuth();
  const isAdmin = !loading && user?.role === 'admin';

  const items: NavItem[] = [
    {
      href: '/search',
      icon: <Search className="h-4 w-4" aria-hidden="true" />,
      label: 'Search Rulings',
      activeFn: (p) => p === '/search',
    },
    {
      href: '/rulings',
      icon: <FileText className="h-4 w-4" aria-hidden="true" />,
      label: 'Latest Rulings',
      activeFn: (p) => p === '/rulings',
    },
    {
      href: '/cases',
      icon: <FolderOpen className="h-4 w-4" aria-hidden="true" />,
      label: 'Cases',
      activeFn: (p) => p === '/cases',
    },
    {
      href: '/judges',
      icon: <Gavel className="h-4 w-4" aria-hidden="true" />,
      label: 'Judges',
      activeFn: (p) => p === '/judges',
    },
  ];

  if (isAdmin) {
    items.push({
      href: '/admin/data-quality/',
      icon: <BarChart3 className="h-4 w-4" aria-hidden="true" />,
      label: 'Data Health',
      activeFn: (p) => p.startsWith('/admin/data-quality'),
      adminOnly: true,
    });
  }

  return items;
}

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

/** Wraps the Sidebar in the desktop aside container. Visible only at lg (>= 1024px). */
export function DesktopSidebar() {
  return (
    <aside aria-label="Sidebar navigation" className="sticky top-14 hidden h-[calc(100vh-3.5rem)] w-56 shrink-0 border-r bg-muted/40 lg:block">
      <Sidebar />
    </aside>
  );
}

/** Icon-only sidebar for tablet viewports (md to lg, 768px–1024px). */
export function TabletSidebar() {
  const pathname = usePathname();
  const navItems = useNavItems();

  /** Index of the first admin-only item, used to insert a separator. */
  const adminStartIdx = navItems.findIndex((item) => item.adminOnly);
  /** Index where "Research" group starts (Cases). */
  const researchStartIdx = navItems.findIndex((item) => item.href === '/cases');

  return (
    <aside
      aria-label="Sidebar navigation"
      className="sticky top-14 hidden h-[calc(100vh-3.5rem)] w-12 shrink-0 border-r bg-muted/40 md:block lg:hidden"
    >
      <TooltipProvider delayDuration={150}>
        <nav aria-label="Sidebar" className="flex flex-col items-center gap-1 py-4">
          {navItems.map((item, idx) => (
            <span key={item.href}>
              {/* Insert separator before Research and Admin groups */}
              {(idx === researchStartIdx || idx === adminStartIdx) && (
                <Separator className="my-2 w-6" />
              )}
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant={item.activeFn(pathname) ? 'secondary' : 'ghost'}
                    size="icon"
                    className={cn(
                      'h-9 w-9',
                      item.activeFn(pathname) &&
                        'bg-accent text-accent-foreground border-l-2 border-primary',
                    )}
                    asChild
                  >
                    <Link href={item.href} aria-label={item.label}>
                      {item.icon}
                    </Link>
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="right" sideOffset={8}>
                  {item.label}
                </TooltipContent>
              </Tooltip>
            </span>
          ))}
        </nav>
      </TooltipProvider>
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
        active && 'bg-accent text-accent-foreground font-medium border-l-2 border-primary',
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
