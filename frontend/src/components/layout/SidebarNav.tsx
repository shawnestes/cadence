import { Ban, GitBranch, LayoutDashboard, Users } from "lucide-react";
import { NavLink } from "react-router-dom";

import { cn } from "@/lib/utils";

const items = [
  { to: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { to: "/recipients", label: "Recipients", icon: Users },
  { to: "/suppressions", label: "Suppressions", icon: Ban },
  { to: "/workflows", label: "Workflows", icon: GitBranch },
];

export function SidebarNav() {
  return (
    <nav className="flex flex-col gap-1">
      {items.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
              isActive ? "bg-accent text-accent-foreground" : "hover:bg-accent/50",
            )
          }
        >
          <Icon className="size-4" />
          {label}
        </NavLink>
      ))}
    </nav>
  );
}
