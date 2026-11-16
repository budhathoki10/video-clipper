"use client";

import { AlertCircle, Check, LoaderCircle, X } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";

export function cx(...parts: (string | false | null | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}

export function Spinner({ className }: { className?: string }) {
  return <LoaderCircle aria-hidden className={cx("animate-spin-slow size-3.5", className)} />;
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "md" | "lg" | "icon";
  loading?: boolean;
};

export function Button({ variant = "secondary", size = "md", loading, disabled, children, className, ...rest }: ButtonProps) {
  return (
    <button
      type="button"
      {...rest}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cx("btn", `btn-${variant}`, size === "lg" && "btn-lg", size === "icon" && "btn-icon", className)}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
}

export function Field({
  label,
  hint,
  error,
  children,
  className,
}: {
  label: string;
  hint?: string;
  error?: string | null;
  children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: boolean }) => ReactNode;
  className?: string;
}) {
  const id = useId();
  const note = error || hint;
  return (
    <div className={cx("flex flex-col gap-1.5", className)}>
      <label htmlFor={id} className="text-xs font-medium text-ink-2">
        {label}
      </label>
      {children({ id, "aria-describedby": note ? `${id}-note` : undefined, "aria-invalid": error ? true : undefined })}
      {note && (
        <p id={`${id}-note`} className={cx("text-xs", error ? "text-bad-ink" : "text-ink-3")}>
          {note}
        </p>
      )}
    </div>
  );
}

export function Toggle({ checked, onChange, label, hint }: { checked: boolean; onChange: (v: boolean) => void; label: string; hint?: string }) {
  const id = useId();
  return (
    <div className="flex items-start justify-between gap-4">
      <label htmlFor={id} className="cursor-pointer">
        <span className="block text-sm text-ink">{label}</span>
        {hint && <span className="block text-xs text-ink-3">{hint}</span>}
      </label>
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={cx(
          "relative mt-0.5 h-5 w-9 flex-none rounded-full border transition-colors duration-150",
          checked ? "border-accent bg-accent" : "border-line-strong bg-line-strong hover:border-ink-3 hover:bg-ink-3/40",
        )}
      >
        <span
          className={cx(
            "absolute top-0.5 left-0.5 size-3.5 rounded-full transition-transform duration-150",
            checked ? "translate-x-4 bg-white shadow-soft" : "bg-white shadow-soft",
          )}
        />
      </button>
    </div>
  );
}

const TONES = {
  neutral: "bg-raised text-ink-2 border-line",
  accent: "bg-accent-soft text-accent border-transparent",
  ok: "bg-ok-soft text-ok-ink border-transparent",
  warn: "bg-warn-soft text-warn-ink border-transparent",
  bad: "bg-bad-soft text-bad-ink border-transparent",
} as const;

export function Badge({ tone = "neutral", children }: { tone?: keyof typeof TONES; children: ReactNode }) {
  return (
    <span className={cx("inline-flex h-5 items-center gap-1 rounded-sm border px-1.5 text-xs font-medium whitespace-nowrap", TONES[tone])}>
      {children}
    </span>
  );
}

export function Progress({ value, label }: { value: number; label: string }) {
  const pct = Math.round(Math.min(Math.max(value, 0), 1) * 100);
  return (
    <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} className="h-1 w-full overflow-hidden rounded-full bg-raised">
      <div className="h-full rounded-full bg-accent transition-[width] duration-500 ease-out" style={{ width: `${pct}%` }} />
    </div>
  );
}

/** Predicted performance, 0-100. Colour carries meaning only: strong, middling, weak. */
export function Score({ value, size = "sm" }: { value: number | null; size?: "sm" | "lg" }) {
  if (value === null) return <span className="text-xs text-ink-3">Not scored</span>;
  const rounded = Math.round(value);
  const tone = rounded >= 70 ? "text-ok-ink" : rounded >= 45 ? "text-warn-ink" : "text-ink-2";
  return (
    <span className={cx("num font-semibold", tone, size === "lg" ? "text-xl tracking-tight" : "text-sm")} title="Predicted performance out of 100">
      {rounded}
      <span className="sr-only"> out of 100</span>
    </span>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-lg tracking-tight">{title}</h1>
        {description && <p className="mt-1 text-sm text-ink-2">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function EmptyState({ icon, title, body, action }: { icon?: ReactNode; title: string; body: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center rounded-lg border border-dashed border-line px-6 py-12 text-center">
      {icon && <div className="mb-3 text-ink-3 [&_svg]:size-5">{icon}</div>}
      <h2 className="text-sm font-medium">{title}</h2>
      <p className="mt-1 max-w-sm text-sm text-ink-2">{body}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border border-bad/40 bg-bad-soft px-4 py-3 text-sm">
      <AlertCircle aria-hidden className="size-4 flex-none text-bad-ink" />
      <p className="min-w-0 flex-1">{message}</p>
      {onRetry && <Button onClick={onRetry}>Try again</Button>}
    </div>
  );
}

export function Notice({ tone = "warn", children }: { tone?: "warn" | "bad" | "accent"; children: ReactNode }) {
  const styles = { warn: "border-warn/40 bg-warn-soft", bad: "border-bad/40 bg-bad-soft", accent: "border-accent/40 bg-accent-soft" };
  return <div className={cx("rounded-md border px-3 py-2 text-sm text-ink", styles[tone])}>{children}</div>;
}

export function SkeletonRows({ rows = 4, height = "h-10" }: { rows?: number; height?: string }) {
  return (
    <div aria-busy="true" aria-label="Loading" className="flex flex-col gap-2">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className={cx("skeleton w-full", height)} />
      ))}
    </div>
  );
}

export function Section({ title, description, children, aside }: { title: string; description?: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="panel">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line-subtle px-4 py-3">
        <div>
          <h2 className="text-sm font-medium">{title}</h2>
          {description && <p className="mt-0.5 text-xs text-ink-3">{description}</p>}
        </div>
        {aside}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

// ---- dialog ----------------------------------------------------------------
export function Dialog({ open, onClose, title, children, footer }: { open: boolean; onClose: () => void; title: string; children: ReactNode; footer?: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    // The native <dialog> traps focus, closes on Esc and restores focus on close.
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onClick={(e) => e.target === ref.current && onClose()}
      className="m-auto w-[min(440px,calc(100vw-32px))] rounded-xl border border-line bg-surface p-0 text-ink shadow-pop backdrop:bg-ink/40 open:animate-enter"
    >
      <div className="flex items-center justify-between border-b border-line-subtle px-4 py-3">
        <h2 className="text-sm font-medium">{title}</h2>
        <Button variant="ghost" size="icon" aria-label="Close" onClick={onClose}>
          <X />
        </Button>
      </div>
      <div className="p-4">{children}</div>
      {footer && <div className="flex justify-end gap-2 border-t border-line-subtle px-4 py-3">{footer}</div>}
    </dialog>
  );
}

// ---- toasts ----------------------------------------------------------------
type Toast = { id: number; tone: "ok" | "bad"; message: string };
const ToastContext = createContext<(message: string, tone?: Toast["tone"]) => void>(() => {});
export const useToast = () => useContext(ToastContext);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((message: string, tone: Toast["tone"] = "ok") => {
    const id = Date.now() + Math.random();
    setToasts((list) => [...list.slice(-2), { id, tone, message }]);
    setTimeout(() => setToasts((list) => list.filter((t) => t.id !== id)), tone === "bad" ? 7000 : 3500);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div aria-live="polite" className="pointer-events-none fixed inset-x-4 bottom-20 z-50 flex flex-col items-center gap-2 md:inset-x-auto md:right-6 md:bottom-6 md:items-end">
        {toasts.map((toast) => (
          <div key={toast.id} role="status" className="animate-enter pointer-events-auto flex max-w-sm items-start gap-2 rounded-md border border-line bg-surface px-3 py-2.5 text-sm shadow-pop">
            {toast.tone === "ok" ? <Check aria-hidden className="mt-0.5 size-4 flex-none text-ok-ink" /> : <AlertCircle aria-hidden className="mt-0.5 size-4 flex-none text-bad-ink" />}
            <span>{toast.message}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
