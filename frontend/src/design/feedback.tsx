import { useEffect, useRef, type ReactNode } from "react";
import { ApiError } from "../api/client";
import { Button } from "./ui";

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return <p role="status" aria-live="polite" className="py-10 text-sm text-ink-soft">{label}</p>;
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="rule py-10">
      <h3 className="font-display text-xl">{title}</h3>
      {children && <p className="mt-2 max-w-prose text-sm text-ink-soft">{children}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "Something went wrong.";
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const apiError = error instanceof ApiError ? error : null;
  return (
    <div role="alert" className="rule py-8">
      <h3 className="font-display text-xl">{apiError?.status === 404 ? "Not found" : "This could not be loaded"}</h3>
      <p className="mt-2 max-w-prose text-sm text-ink-soft">{errorMessage(error)}</p>
      {apiError?.requestId && <p className="mono mt-2 text-ink-soft">Reference {apiError.requestId}</p>}
      {onRetry && (
        <div className="mt-4">
          <Button variant="secondary" onClick={onRetry}>Try again</Button>
        </div>
      )}
    </div>
  );
}

/** Inline outcome of a mutation (success or error), announced to assistive technology. */
export function Notice({ tone, children }: { tone: "error" | "success" | "info"; children: ReactNode }) {
  const cls =
    tone === "error"
      ? "border-terracotta-deep bg-terracotta-wash text-terracotta-deep"
      : tone === "success"
        ? "border-sage-deep/50 bg-sage-wash text-sage-deep"
        : "border-stone bg-paper text-ink-soft";
  return (
    <p role={tone === "error" ? "alert" : "status"} className={`border-l-2 px-3 py-2 text-sm ${cls}`}>
      {children}
    </p>
  );
}

interface ConfirmProps {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  busy?: boolean;
  destructive?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Accessible modal confirmation for consequential actions. Focus moves into the dialog and Escape cancels. */
export function ConfirmationDialog({ open, title, children, confirmLabel, busy, destructive, onConfirm, onCancel }: ConfirmProps) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal?.();
    if (!open && el.open) el.close?.();
  }, [open]);
  if (!open) return null;
  return (
    <dialog
      ref={ref}
      aria-labelledby="confirm-title"
      onCancel={(e) => {
        e.preventDefault();
        onCancel();
      }}
      className="w-[min(32rem,92vw)] border border-stone bg-parchment p-0 text-ink shadow-[0_12px_40px_-20px_rgba(51,51,47,0.45)] backdrop:bg-ink/30"
    >
      <div className="space-y-4 p-6">
        <h2 id="confirm-title" className="font-display text-2xl">{title}</h2>
        <div className="space-y-2 text-sm text-ink-soft">{children}</div>
        <div className="flex justify-end gap-3 pt-2">
          <Button variant="secondary" onClick={onCancel}>Cancel</Button>
          <Button variant={destructive ? "danger" : "primary"} busy={busy} onClick={onConfirm}>{confirmLabel}</Button>
        </div>
      </div>
    </dialog>
  );
}
