import { cn } from "@/lib/utils";

export type BadgeVariant =
  | "default"
  | "secondary"
  | "success"
  | "warning"
  | "destructive";

const variants: Record<BadgeVariant, string> = {
  default: "border border-border text-foreground",
  secondary: "border border-border text-muted-foreground",
  success: "border border-success text-success",
  warning: "border border-warn text-warn",
  destructive: "border border-destructive text-destructive",
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
        "inline-flex items-center rounded px-1.5 py-0.5 text-[0.65rem] font-mono font-medium uppercase tracking-[0.08em]",
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
