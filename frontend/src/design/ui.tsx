import { AlertTriangle, Check, Info, OctagonX, type LucideIcon } from "lucide-react";
import { forwardRef, useId, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";

export const cx = (...parts: Array<string | false | null | undefined>) => parts.filter(Boolean).join(" ");

/* ---------- Button ---------- */
type ButtonVariant = "primary" | "secondary" | "quiet" | "danger";
const buttonBase = "inline-flex items-center justify-center gap-2 px-4 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 rounded";
const buttonVariants: Record<ButtonVariant, string> = {
  primary: "bg-terracotta text-white hover:bg-terracotta-deep",
  secondary: "border border-ink/40 text-ink hover:bg-paper",
  quiet: "text-terracotta-deep hover:bg-terracotta-wash px-2",
  danger: "border border-terracotta-deep text-terracotta-deep hover:bg-terracotta-wash",
};
export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  busy?: boolean;
}
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(({ variant = "primary", busy, className, children, disabled, type = "button", ...rest }, ref) => (
  <button ref={ref} type={type} disabled={disabled || busy} aria-busy={busy || undefined} className={cx(buttonBase, buttonVariants[variant], className)} {...rest}>
    {busy ? "Working…" : children}
  </button>
));
Button.displayName = "Button";

/* ---------- Fields ---------- */
const fieldControl = "w-full border border-stone bg-white px-3 py-2 text-sm text-ink placeholder:text-ink-soft/70 rounded focus:border-terracotta-deep";
interface FieldShellProps {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  children: (id: string, describedBy?: string) => ReactNode;
}
function FieldShell({ label, hint, error, children }: FieldShellProps) {
  const id = useId();
  const describedBy = error ? `${id}-err` : hint ? `${id}-hint` : undefined;
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-sm font-medium">{label}</label>
      {children(id, describedBy)}
      {error ? (
        <p id={`${id}-err`} role="alert" className="text-xs text-terracotta-deep">{error}</p>
      ) : hint ? (
        <p id={`${id}-hint`} className="text-xs text-ink-soft">{hint}</p>
      ) : null}
    </div>
  );
}
type FieldExtras = { label: string; hint?: ReactNode; error?: string | null; mono?: boolean };
export const InputField = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & FieldExtras>(({ label, hint, error, mono, className, ...rest }, ref) => (
  <FieldShell label={label} hint={hint} error={error}>
    {(id, d) => <input ref={ref} id={id} aria-describedby={d} aria-invalid={!!error || undefined} className={cx(fieldControl, mono && "mono", className)} {...rest} />}
  </FieldShell>
));
InputField.displayName = "InputField";
export function TextAreaField({ label, hint, error, mono, className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement> & FieldExtras) {
  return (
    <FieldShell label={label} hint={hint} error={error}>
      {(id, d) => <textarea id={id} aria-describedby={d} aria-invalid={!!error || undefined} className={cx(fieldControl, "min-h-28", mono && "mono", className)} {...rest} />}
    </FieldShell>
  );
}
export function SelectField({ label, hint, error, children, className, ...rest }: SelectHTMLAttributes<HTMLSelectElement> & Omit<FieldExtras, "mono">) {
  return (
    <FieldShell label={label} hint={hint} error={error}>
      {(id, d) => <select id={id} aria-describedby={d} className={cx(fieldControl, className)} {...rest}>{children}</select>}
    </FieldShell>
  );
}

/* ---------- StatusLabel: never colour alone (icon + text on every state) ---------- */
export type Tone = "verified" | "attention" | "blocked" | "advisory" | "neutral";
const toneStyle: Record<Tone, { cls: string; Icon: LucideIcon | null }> = {
  verified: { cls: "border-sage-deep/40 bg-sage-wash text-sage-deep", Icon: Check },
  attention: { cls: "border-terracotta-deep/40 bg-terracotta-wash text-terracotta-deep", Icon: AlertTriangle },
  blocked: { cls: "border-ink bg-ink text-parchment", Icon: OctagonX },
  advisory: { cls: "border-stone bg-paper text-ink-soft", Icon: Info },
  neutral: { cls: "border-stone text-ink-soft", Icon: null },
};
export function StatusLabel({ tone = "neutral", children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  const { cls, Icon } = toneStyle[tone];
  return (
    <span className={cx("inline-flex items-center gap-1.5 whitespace-nowrap rounded-sm border px-2 py-0.5 text-xs font-medium", cls, className)}>
      {Icon ? <Icon aria-hidden className="h-3.5 w-3.5" /> : <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}
