import { Outlet } from "react-router-dom";

import { SidebarNav } from "@/components/layout/SidebarNav";

export function AppShell() {
  return (
    <div className="flex min-h-screen">
      <aside className="w-56 border-r bg-muted/30 p-4">
        <h1 className="mb-6 text-lg font-semibold">Cadence</h1>
        <SidebarNav />
      </aside>
      <main className="flex-1 p-8">
        <Outlet />
      </main>
    </div>
  );
}
