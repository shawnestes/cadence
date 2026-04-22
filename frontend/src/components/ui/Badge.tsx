import { cn } from "@/lib/utils";

export type BadgeVariant =
  | "default"
  | "secondary"
  | "success"
  | "warning"
  | "destructive";

const variants: Record<BadgeVariant, string> = {
  default: "bg-secondary text-secondary-foreground",
  secondary: "bg-muted text-muted-foreground",
  success: "bg-green-100 text-green-800",
  warning: "bg-yellow-100 text-yellow-800",
  destructive: "bg-destructive/10 text-destructive",
};

type Props = {
  children: React.ReactNode;
  variant?: BadgeVariant;
  className?: string;
};

export function Badge({ children, variant = "default", className }: Props) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium",
        variants[variant],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function statusVariant(status: string): BadgeVariant {
  switch (status) {
    case "active":
    case "approved":
    case "delivered":
      return "success";
    case "paused":
    case "generating":
      return "warning";
    case "rejected":
    case "retired":
    case "archived":
      return "secondary";
    case "bounced":
    case "complained":
    case "failed":
      return "destructive";
    case "draft":
    case "ready":
    default:
      return "default";
  }
}
