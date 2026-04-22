import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";
import { Outlet } from "react-router-dom";

import { TopNav } from "@/components/layout/SidebarNav";

function useTheme() {
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    if (typeof localStorage !== "undefined") {
      const stored = localStorage.getItem("cadence-theme");
      if (stored === "light") return "light";
    }
    return "dark";
  });

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "dark") {
      root.classList.add("dark");
    } else {
      root.classList.remove("dark");
    }
    localStorage.setItem("cadence-theme", theme);
  }, [theme]);

  const toggle = () => setTheme((t) => (t === "dark" ? "light" : "dark"));
  return { theme, toggle };
}

export function AppShell() {
  const { theme, toggle } = useTheme();

  return (
    <div className="min-h-screen bg-background text-foreground flex flex-col">
      <header className="sticky top-0 z-20 border-b border-border bg-surface/95 backdrop-blur supports-[backdrop-filter]:bg-surface/80">
        <div className="flex items-center h-14 px-6 gap-8">
          <div className="flex items-baseline">
            <span className="font-sans text-base font-bold uppercase tracking-[0.35em] text-foreground leading-none">
              CADENCE
            </span>
          </div>
          <TopNav />
          <div className="ml-auto">
            <button
              onClick={toggle}
              className="flex items-center gap-2 px-3 py-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
              aria-label="Toggle theme"
            >
              {theme === "dark" ? (
                <Sun className="size-4" />
              ) : (
                <Moon className="size-4" />
              )}
              <span className="text-[0.65rem] font-sans font-semibold uppercase tracking-[0.2em]">
                {theme === "dark" ? "Light" : "Dark"}
              </span>
            </button>
          </div>
        </div>
      </header>

      <main className="flex-1 min-w-0 px-6 py-6">
        <Outlet />
      </main>
    </div>
  );
}
