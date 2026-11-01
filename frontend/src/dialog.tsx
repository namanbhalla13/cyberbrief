/** A modal dialog and a toast, as in the journey design.
 *
 * The dialog is the platform `<dialog>`: focus moves into it when it opens, Escape closes it, the page behind is inert, and focus returns
 * to the control that opened it. A toast says what was actually committed (it is shown after the server answered, never before) and is
 * announced politely; anything that matters stays on the page as well.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Icon } from "./icons";
import { useI18n } from "./ui";

export function Dialog({ title, open, onClose, children, footer }: { title: string; open: boolean; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  const { t } = useI18n();
  const ref = useRef<HTMLDialogElement | null>(null);
  const opener = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) {
      opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      el.showModal();
    } else if (!open && el.open) {
      el.close();
    }
  }, [open]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const done = () => { onClose(); opener.current?.focus?.(); };
    el.addEventListener("close", done);
    return () => el.removeEventListener("close", done);
  }, [onClose]);

  return <dialog ref={ref} className="dialog" aria-labelledby="dialog-title" onClick={(e) => { if (e.target === ref.current) onClose(); }}>
    {open && <>
      <div className="dialoghead"><h2 id="dialog-title">{title}</h2>
        <button type="button" className="secondary icon-button" aria-label={t("Close dialog")} title={t("Close")} onClick={onClose}><Icon name="close" size={18}/></button></div>
      <div className="dialogbody">{children}</div>
      {footer && <div className="dialogfoot">{footer}</div>}
    </>}
  </dialog>;
}

interface ToastApi { say: (text: string) => void }
const ToastContext = createContext<ToastApi>({ say: () => undefined });

export function ToastProvider({ children }: { children: ReactNode }) {
  const [text, setText] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const say = useCallback((message: string) => {
    setText(message);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setText(null), 5000);
  }, []);
  return <ToastContext.Provider value={{ say }}>
    {children}
    <div className="toast" role="status" aria-live="polite" hidden={text === null}>{text}</div>
  </ToastContext.Provider>;
}

export const useToast = () => useContext(ToastContext);
