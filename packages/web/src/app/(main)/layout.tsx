import { DesktopSidebar } from '@/components/layout/Sidebar';

export default function MainLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-1">
      <DesktopSidebar />
      <main id="main-content" className="min-w-0 flex-1 p-6">{children}</main>
    </div>
  );
}
