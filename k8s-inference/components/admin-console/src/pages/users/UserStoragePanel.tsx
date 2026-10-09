import { useState } from "react";
import { envelopeRequest } from "../../api/client";
import type { UserStorage } from "../../api/userTypes";

type Credentials = {
  bucket_name: string;
  endpoint: string;
  region: string;
  access_key_id: string;
  secret_access_key: string;
};

export function UserStoragePanel({ userId, storage, canReveal }: {
  userId: string; storage: UserStorage; canReveal: boolean;
}) {
  const [credentials, setCredentials] = useState<Credentials | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function reveal() {
    setBusy(true);
    setError(null);
    try {
      const response = await envelopeRequest<Credentials>(
        `/users/${encodeURIComponent(userId)}/storage/credentials`, { method: "POST" },
      );
      setCredentials(response.data);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not retrieve S3 credentials");
    } finally {
      setBusy(false);
    }
  }

  return <section className="panel section-stack">
    <h3>Data storage</h3>
    <p>{storage.mode === "user" ? "Private user bucket" : "Shared tenant bucket"} · {storage.state}</p>
    <dl>
      <dt>Storage allowance</dt><dd>{storage.quota_bytes / 1_000_000_000} GB</dd>
      {storage.bucket_name && <><dt>Bucket</dt><dd><code>{storage.bucket_name}</code></dd></>}
      {storage.endpoint && <><dt>S3 endpoint</dt><dd><code>{storage.endpoint}</code></dd></>}
      {storage.region && <><dt>Region</dt><dd>{storage.region}</dd></>}
      {storage.access_key_id && <><dt>Access key ID</dt><dd><code>{storage.access_key_id}</code></dd></>}
    </dl>
    {storage.state === "pending" && <p>Provisioning automatically. Refresh shortly.</p>}
    {storage.state === "disabled" && <p>Storage is disabled for this tenant or user.</p>}
    {canReveal && storage.state === "ready" && !credentials &&
      <button className="button" disabled={busy} onClick={() => void reveal()}>
        {busy ? "Loading…" : "Show S3 credentials"}
      </button>}
    {credentials && <>
      <label htmlFor="s3-connection">S3 connection details</label>
      <textarea id="s3-connection" readOnly rows={8} value={JSON.stringify(credentials, null, 2)} />
      <button className="button" onClick={() => setCredentials(null)}>Hide credentials</button>
    </>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
