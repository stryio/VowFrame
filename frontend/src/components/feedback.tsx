import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { ApiError } from "../api";

// ---------- Toast ----------

interface Toast {
  id: number;
  message: string;
  kind: "info" | "error";
}

interface ToastApi {
  info: (message: string) => void;
  error: (err: unknown) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((message: string, kind: Toast["kind"]) => {
    const id = Date.now() + Math.random();
    setToasts((list) => [...list, { id, message, kind }]);
    window.setTimeout(() => setToasts((list) => list.filter((t) => t.id !== id)), 3600);
  }, []);

  const api: ToastApi = {
    info: (message) => push(message, "info"),
    // Only ApiError messages are written for end users; anything else gets a plain fallback.
    error: (err) =>
      push(err instanceof ApiError ? err.message : "操作失败，请稍后重试", "error"),
  };

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toasts">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.kind === "error" ? "toast-error" : ""}`}>
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside ToastProvider");
  return ctx;
}

// ---------- Confirm dialog ----------

export interface ConfirmOptions {
  title: string;
  message: ReactNode;
  confirmText?: string;
  danger?: boolean;
}

export function ConfirmDialog({
  options,
  onConfirm,
  onCancel,
}: {
  options: ConfirmOptions;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="backdrop" onClick={onCancel}>
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <h2>{options.title}</h2>
        <p>{options.message}</p>
        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onCancel}>
            取消
          </button>
          <button
            className={`btn ${options.danger ? "btn-danger" : "btn-primary"}`}
            onClick={onConfirm}
            autoFocus
          >
            {options.confirmText ?? "确定"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Imperative-style confirm: `const ok = await confirm({...})`. */
export function useConfirm() {
  const [pending, setPending] = useState<{
    options: ConfirmOptions;
    resolve: (ok: boolean) => void;
  } | null>(null);

  const confirm = useCallback(
    (options: ConfirmOptions) =>
      new Promise<boolean>((resolve) => setPending({ options, resolve })),
    [],
  );

  const close = (ok: boolean) => {
    pending?.resolve(ok);
    setPending(null);
  };

  const dialog = pending ? (
    <ConfirmDialog
      options={pending.options}
      onConfirm={() => close(true)}
      onCancel={() => close(false)}
    />
  ) : null;

  return { confirm, dialog };
}

// ---------- Empty state (PRD 15.8: one shared component) ----------

export function EmptyState({
  icon = "✦",
  title,
  description,
  action,
}: {
  icon?: string;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="card empty">
      <div className="empty-icon">{icon}</div>
      <h2>{title}</h2>
      {description && <p>{description}</p>}
      {action}
    </div>
  );
}

// ---------- Lightbox ----------

export function Lightbox({
  src,
  caption,
  onClose,
}: {
  src: string;
  caption?: string;
  onClose: () => void;
}) {
  return (
    <div className="backdrop lightbox" onClick={onClose}>
      <button className="lightbox-close" onClick={onClose} aria-label="关闭">
        ×
      </button>
      <div className="lightbox-inner" onClick={(e) => e.stopPropagation()}>
        <img src={src} alt={caption ?? ""} />
        {caption && <div className="lightbox-caption">{caption}</div>}
      </div>
    </div>
  );
}
