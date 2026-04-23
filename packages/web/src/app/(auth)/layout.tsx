export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <main id="main-content" className="flex flex-1 items-center justify-center p-6">
      {children}
    </main>
  );
}
