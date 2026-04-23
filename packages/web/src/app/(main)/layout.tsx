import { DesktopSidebar, TabletSidebar } from '@/components/layout/Sidebar';

export default function MainLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-1">
      {/* Icon-only sidebar at md–lg (768–1024px); full sidebar at lg+ */}
      <TabletSidebar />
      <DesktopSidebar />
      <main id="main-content" className="min-w-0 flex-1 p-6">{children}</main>
    </div>
  );
}
