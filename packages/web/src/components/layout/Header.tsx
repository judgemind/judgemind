'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Menu, Moon, Sun, LogOut, User } from 'lucide-react';
import { useTheme } from '@/providers/ThemeProvider';
import { useAuth } from '@/providers/AuthProvider';
import { Sidebar } from '@/components/layout/Sidebar';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';

export function Header() {
  const { theme, toggle } = useTheme();
  const { user, loading, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <>
      <header className="sticky top-0 z-50 flex h-14 items-center border-b bg-background px-4">
        {/* Hamburger — visible only on mobile (below lg breakpoint) */}
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setMenuOpen(true)}
          aria-label="Toggle menu"
          className="mr-2 lg:hidden"
        >
          <Menu className="h-5 w-5" aria-hidden="true" />
        </Button>

        <Link href="/" className="mr-8 rounded-md text-lg font-semibold text-brand-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 dark:text-brand-accent-light">
          Judgemind
        </Link>

        <nav aria-label="Main" className="hidden flex-1 items-center gap-1 text-sm font-medium md:flex">
          <Button variant="ghost" size="sm" asChild>
            <Link href="/search">Search</Link>
          </Button>
          <Button variant="ghost" size="sm" asChild>
            <Link href="/rulings">Rulings</Link>
          </Button>
        </nav>

        {/* Spacer for mobile (no nav links shown) */}
        <div className="flex-1 md:hidden" />

        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            onClick={toggle}
            aria-label="Toggle dark mode"
          >
            {theme === 'dark' ? (
              <Sun className="h-5 w-5" aria-hidden="true" />
            ) : (
              <Moon className="h-5 w-5" aria-hidden="true" />
            )}
          </Button>

          {!loading && (
            <>
              {user ? (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="ghost" size="icon" aria-label="User menu">
                      <User className="h-5 w-5" aria-hidden="true" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-48">
                    <DropdownMenuLabel className="font-normal">
                      <p className="text-sm font-medium">{user.displayName ?? 'Account'}</p>
                      <p className="text-xs text-muted-foreground">{user.email}</p>
                    </DropdownMenuLabel>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem onClick={() => void logout()}>
                      <LogOut className="mr-2 h-4 w-4" aria-hidden="true" />
                      Log out
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              ) : (
                <Button size="sm" asChild>
                  <Link href="/auth/login">Log in</Link>
                </Button>
              )}
            </>
          )}
        </div>
      </header>

      {/* Mobile slide-out menu using shadcn Sheet */}
      <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
        <SheetContent side="left" className="w-64 p-0">
          <SheetHeader className="border-b px-4 py-3">
            <SheetTitle className="text-lg font-semibold text-brand-accent dark:text-brand-accent-light">
              Judgemind
            </SheetTitle>
          </SheetHeader>
          <Sidebar onLinkClick={() => setMenuOpen(false)} />
        </SheetContent>
      </Sheet>
    </>
  );
}
