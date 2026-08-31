import { useState } from "react";
import type { AdminApiKeyDisclosure } from "../api/accessTypes";
import { Modal } from "./Modal";

interface Props {
  disclosure: AdminApiKeyDisclosure;
  onDismiss: () => void;
}

export function OneTimeSecretDialog({ disclosure, onDismiss }: Props) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard API unavailable");
      await navigator.clipboard.writeText(disclosure.secret);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  }

  return (
    <Modal
      closeLabel="Dismiss one-time API key"
      description="This is the only response that contains the credential. Copy it now; closing this view permanently clears it from console state."
      onClose={onDismiss}
      title="API key created"
    >
      <div className="secret-warning" role="status">
        <strong>One-time disclosure</strong>
        <span>The raw key is never written to browser storage, URLs, telemetry or later list responses.</span>
      </div>
      <label className="secret-field">
        <span>Key for {disclosure.key.name ?? disclosure.key.prefix}</span>
        <input
          aria-describedby="copy-result"
          autoComplete="off"
          readOnly
          spellCheck={false}
          type="text"
          value={disclosure.secret}
        />
      </label>
      <div className="modal-actions">
        <span aria-live="polite" id="copy-result">
          {copyState === "copied" ? "Copied. Store it in your approved secret manager." : null}
          {copyState === "failed" ? "Clipboard access failed. Select and copy the value manually." : null}
        </span>
        <button className="button" onClick={() => void copy()} type="button">Copy key</button>
        <button className="button button--primary" onClick={onDismiss} type="button">I have stored it</button>
      </div>
    </Modal>
  );
}
