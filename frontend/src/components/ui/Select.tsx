import { cn } from "@/lib/utils";

export type SelectOption = { label: string; value: string };

type Props = {
  options: SelectOption[];
  value: string;
  onChange: (v: string) => void;
  className?: string;
  disabled?: boolean;
};

export function Select({
  options,
  value,
  onChange,
  className,
  disabled,
}: Props) {
  return (
    <select
      className={cn(
        "h-10 rounded-md border border-input bg-background px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50",
        className,
      )}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
