import { useState } from "react";
import type { TerraformHandoff } from "../api/configurationTypes";
import { Modal } from "./Modal";

interface Props {
  handoff: TerraformHandoff;
  onDismiss: () => void;
}

export function TerraformHandoffDialog({ handoff, onDismiss }: Props) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard API unavailable");
      await navigator.clipboard.writeText(handoff.tfvars_json);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  }

  function download() {
    const url = URL.createObjectURL(new Blob([handoff.tfvars_json], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = handoff.tfvars_filename;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <Modal
      closeLabel="Dismiss one-time Terraform handoff"
      description="Copy or download this deterministic, secret-free tfvars document now. Closing this dialog clears the document from console state; create a new plan to disclose it again."
      eyebrow="Declarative configuration handoff"
      onClose={onDismiss}
      title="Terraform handoff ready"
    >
      <div className="secret-warning" role="status">
        <strong>Review-only browser boundary</strong>
        <span>The console cannot run Terraform, mutate cloud resources or patch Kubernetes. An operator must review this file and use the approved Terraform workflow.</span>
      </div>
      <dl className="definition-grid handoff-identities">
        <div><dt>Filename</dt><dd><code>{handoff.tfvars_filename}</code></dd></div>
        <div><dt>tfvars SHA-256</dt><dd><code title={handoff.tfvars_sha256}>{handoff.tfvars_sha256.slice(0, 16)}…</code></dd></div>
        <div><dt>Variables SHA-256</dt><dd><code title={handoff.variables_sha256}>{handoff.variables_sha256.slice(0, 16)}…</code></dd></div>
        <div><dt>Expected source</dt><dd><code title={handoff.expected_source_etag}>{handoff.expected_source_etag.slice(0, 16)}…</code></dd></div>
      </dl>
      <label className="handoff-field">
        <span>Deterministic tfvars JSON</span>
        <textarea aria-describedby="handoff-copy-result" readOnly spellCheck={false} value={handoff.tfvars_json} />
      </label>
      <div className="modal-actions">
        <span aria-live="polite" id="handoff-copy-result">
          {copyState === "copied" ? "Copied. Continue in the approved Terraform workflow." : null}
          {copyState === "failed" ? "Clipboard access failed. Download the file or select the text manually." : null}
        </span>
        <button className="button" onClick={() => void copy()} type="button">Copy tfvars</button>
        <button className="button" onClick={download} type="button">Download tfvars</button>
        <button className="button button--primary" onClick={onDismiss} type="button">Done</button>
      </div>
    </Modal>
  );
}
