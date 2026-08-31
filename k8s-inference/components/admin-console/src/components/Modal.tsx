import { type ReactNode, useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";

interface ModalProps {
  title: string;
  description?: string;
  eyebrow?: string;
  children: ReactNode;
  onClose: () => void;
  closeLabel?: string;
}

export function Modal({
  title,
  description,
  eyebrow = "FS2 Serve access control",
  children,
  onClose,
  closeLabel = "Close",
}: ModalProps) {
  const titleId = useId();
  const descriptionId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);
  const cardRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const priorFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const appShell = document.querySelector<HTMLElement>(".app-shell");
    const priorAriaHidden = appShell?.getAttribute("aria-hidden");
    const wasInert = appShell?.hasAttribute("inert") ?? false;
    appShell?.setAttribute("aria-hidden", "true");
    appShell?.setAttribute("inert", "");
    closeRef.current?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onCloseRef.current();
      if (event.key !== "Tab") return;
      const focusable = [...(cardRef.current?.querySelectorAll<HTMLElement>(
        "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex='-1'])",
      ) ?? [])].filter((element) => !element.hasAttribute("hidden"));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (appShell) {
        priorAriaHidden == null ? appShell.removeAttribute("aria-hidden") : appShell.setAttribute("aria-hidden", priorAriaHidden);
        if (!wasInert) appShell.removeAttribute("inert");
      }
      priorFocus?.focus();
    };
  }, []);

  return createPortal(
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section
        aria-describedby={description ? descriptionId : undefined}
        aria-labelledby={titleId}
        aria-modal="true"
        className="modal-card"
        ref={cardRef}
        role="dialog"
      >
        <header className="modal-card__header">
          <div>
            <span className="eyebrow">{eyebrow}</span>
            <h2 id={titleId}>{title}</h2>
            {description ? <p id={descriptionId}>{description}</p> : null}
          </div>
          <button aria-label={closeLabel} className="icon-button" onClick={onClose} ref={closeRef} type="button">×</button>
        </header>
        {children}
      </section>
    </div>,
    document.body,
  );
}
