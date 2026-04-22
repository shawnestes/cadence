import { NavLink } from "react-router-dom";

import { cn } from "@/lib/utils";

const items = [
  { to: "/dashboard", label: "Dashboard" },
  { to: "/recipients", label: "Recipients" },
  { to: "/suppressions", label: "Suppressions" },
  { to: "/campaigns", label: "Campaigns" },
  { to: "/workflows", label: "Workflows" },
];

export function TopNav() {
  return (
    <nav className="flex items-stretch h-14 gap-1">
      {items.map(({ to, label }) => (
        <NavLink
          key={to}
          to={to}
          className={({ isActive }) =>
            cn(
              "relative flex items-center px-3 text-sm font-sans font-medium tracking-wide transition-colors",
              isActive
                ? "text-foreground after:absolute after:left-3 after:right-3 after:bottom-0 after:h-[2px] after:bg-primary"
                : "text-muted-foreground hover:text-foreground",
            )
          }
        >
          {label}
        </NavLink>
      ))}
    </nav>
  );
}
