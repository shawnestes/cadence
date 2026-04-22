import { cn } from "@/lib/utils";

type Props = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "default" | "outline" | "destructive";
  size?: "sm" | "md";
};

export function Button({
  variant = "default",
  size = "md",
  className,
  ...props
}: Props) {
  const base =
    "inline-flex items-center justify-center rounded-md font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50 disabled:pointer-events-none";
  const sizes = { sm: "h-8 px-3 text-sm", md: "h-10 px-4 text-sm" };
  const variants = {
    default: "bg-primary text-primary-foreground hover:bg-primary/90",
    outline: "border border-border bg-background hover:bg-accent",
    destructive:
      "bg-destructive text-destructive-foreground hover:bg-destructive/90",
  };
  return (
    <button
      className={cn(base, sizes[size], variants[variant], className)}
      {...props}
    />
  );
}
