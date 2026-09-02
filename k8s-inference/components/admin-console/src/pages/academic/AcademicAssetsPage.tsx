import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { adminApi } from "../../api/client";
import type { AcademicAssetReadiness } from "../../api/scientificTypes";
import { DataBoundary } from "../../components/DataBoundary";
import { sharedContextParams } from "../../lib/search";
import { ScientificStatusChip, shortDigest } from "../scientific/ScientificPresentation";

type OperationalStage = "verified" | "unverified" | "blocked";

function stageState(value: string): OperationalStage {
  if (value === "InvalidEvidence") return "blocked";
  if (value.startsWith("Missing") || value === "NotAuthorized") return "unverified";
  return "verified";
}

function assetState(state: AcademicAssetReadiness["state"]): OperationalStage {
  if (state === "Ready") return "verified";
  if (state.startsWith("Invalid")) return "blocked";
  return "unverified";
}

function OperationalStageLine({ label, value }: { label: string; value: string }) {
  return (
    <span className="academic-stage-line">
      <span>{label}</span>
      <ScientificStatusChip
        state={stageState(value)}
        label={value}
        reason={`Operational stage ${label}: ${value}. This is proof-of-concept execution readiness only.`}
      />
    </span>
  );
}

function FormalLicenceChip({ status }: { status: AcademicAssetReadiness["formal_license_status"] }) {
  const pending = status === "FormalAcceptancePending";
  return (
    <span
      className={`scientific-state ${pending ? "scientific-state--formal-acceptance-pending" : "scientific-state--formal-acceptance-recorded"}`}
      title={pending
        ? "No named representative has bound an institution under the licensor's terms."
        : "A named representative has bound a named institution under the licensor's terms."}
    >
      <span className="scientific-state__dot" aria-hidden="true" />
      {status}
    </span>
  );
}

export function AcademicAssetsPage() {
  const [searchParams] = useSearchParams();
  const context = sharedContextParams(searchParams);
  const query = useQuery({
    queryKey: ["admin-academic-assets", context.toString()],
    queryFn: ({ signal }) => adminApi.academicAssets(context, signal),
  });

  return (
    <div className="page-stack scientific-page academic-page">
      <DataBoundary data={query.data} error={query.error} pending={query.isPending}>
        {({ data }) => (
          <div className="page-stack">
            <section className="panel scientific-intro academic-summary" aria-labelledby="academic-assets-intro-title">
              <div>
                <span className="eyebrow">Licensed academic assets</span>
                <h2 id="academic-assets-intro-title">Licensed academic asset readiness</h2>
                <p>
                  Operational readiness and formal licence acceptance are independent facts. Operational readiness only
                  states whether the proof-of-concept path can run; it never records that an institution accepted the
                  licensor&rsquo;s terms.
                </p>
                <dl className="definition-grid academic-delivery-grid">
                  <div><dt>Tenant</dt><dd>{data.tenant_id}</dd></div>
                  <div><dt>Institution</dt><dd>{data.institution_id ?? "No institution bound"}</dd></div>
                  <div><dt>Canonical namespace</dt><dd>{data.delivery.namespace}</dd></div>
                  <div><dt>Canonical claim</dt><dd>{data.delivery.claim}</dd></div>
                  <div><dt>Canonical mount root</dt><dd>{data.delivery.mount_root}</dd></div>
                  <div>
                    <dt>General shared cache</dt>
                    <dd>{data.delivery.general_shared_cache === false ? "Not used" : "Used"}</dd>
                  </div>
                </dl>
              </div>
              <div className="academic-summary__states">
                <span className="academic-axis-label">Operational axis</span>
                <ScientificStatusChip
                  state={data.runtime_path_state === "Ready" ? "verified" : "blocked"}
                  label={`Runtime path ${data.runtime_path_state}`}
                  reason="Whether the proof-of-concept execution path can run for this tenant."
                />
                <span className="academic-axis-label">Formal licence axis</span>
                <FormalLicenceChip
                  status={data.formal_license_state === "Recorded" ? "FormalAcceptanceRecorded" : "FormalAcceptancePending"}
                />
              </div>
            </section>

            {data.formal_license_state === "Pending" ? (
              <section className="inline-notice inline-notice--warning academic-licence-notice" role="status" aria-labelledby="academic-licence-title">
                <strong id="academic-licence-title">Formal acceptance pending: institutional licence acceptance is a separate step.</strong>
                <p>
                  No named representative has yet bound a named institution under the licensor&rsquo;s terms. Nothing on
                  this page records licence acceptance. Operational readiness — authorization, artifact verification,
                  tenant cache, runtime validation, deployment, and semantic readiness — can be fully satisfied while
                  formal acceptance stays pending, and must never be reported as licence acceptance.
                </p>
              </section>
            ) : null}

            <section className="section-stack" aria-labelledby="academic-asset-table-title">
              <div className="section-heading">
                <div>
                  <span className="eyebrow">Per asset</span>
                  <h2 id="academic-asset-table-title">Asset readiness ledger</h2>
                </div>
                <span className="section-heading__meta">{data.items.length} licensed assets</span>
              </div>
              <div className="table-frame">
                <table className="resource-table resource-table--academic-assets">
                  <caption className="sr-only">
                    Licensed academic assets with independent operational readiness and formal licence acceptance state
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Model</th>
                      <th scope="col">State</th>
                      <th scope="col">Operational stages</th>
                      <th scope="col">Authorization</th>
                      <th scope="col">Formal licence</th>
                      <th scope="col">Delivery</th>
                      <th scope="col">Digests</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((asset) => (
                      <tr key={asset.asset_id}>
                        <th scope="row">
                          {asset.display_name}
                          <span className="secondary-line">{asset.model_id} · {asset.backend_id}</span>
                          {asset.alternative ? (
                            <span className="scientific-alternative">
                              Independent alternative <strong>{asset.alternative.model_id}</strong>: a different model,
                              not {asset.model_id}. {asset.alternative.reason}
                            </span>
                          ) : (
                            <span className="secondary-line">No alternative model is published.</span>
                          )}
                        </th>
                        <td>
                          <ScientificStatusChip
                            state={assetState(asset.state)}
                            label={asset.state}
                            reason={`Operational state ${asset.state}. This state does not describe formal licence acceptance.`}
                          />
                          <span className="secondary-line scientific-secondary">Operational readiness only</span>
                        </td>
                        <td>
                          <div className="academic-stage-lines">
                            <OperationalStageLine label="Artifact" value={asset.artifact_status} />
                            <OperationalStageLine label="Tenant cache" value={asset.tenant_cache_status} />
                            <OperationalStageLine label="Runtime" value={asset.runtime_status} />
                            <OperationalStageLine label="Deployment" value={asset.deployment_status} />
                            <OperationalStageLine label="Semantic" value={asset.semantic_status} />
                          </div>
                        </td>
                        <td>
                          <div className="academic-stage-lines">
                            <OperationalStageLine label="Use" value={asset.use_authorization_status} />
                            <OperationalStageLine label="Execution" value={asset.execution_authorization_status} />
                          </div>
                          <span className="secondary-line scientific-secondary">
                            {asset.authorization_receipt_sha256
                              ? `Authorization receipt ${shortDigest(asset.authorization_receipt_sha256)}`
                              : "No authorization receipt digest"}
                          </span>
                        </td>
                        <td>
                          <FormalLicenceChip status={asset.formal_license_status} />
                          <span className="secondary-line scientific-secondary">
                            {asset.formal_license_status === "FormalAcceptancePending"
                              ? "Formal acceptance pending: no named representative has bound an institution. Operational readiness does not satisfy this."
                              : "Formal acceptance recorded by a named representative for a named institution."}
                          </span>
                          <span className="secondary-line scientific-secondary">
                            {asset.acceptance_receipt_sha256
                              ? `Acceptance receipt ${shortDigest(asset.acceptance_receipt_sha256)}`
                              : "No acceptance receipt digest"}
                          </span>
                        </td>
                        <td>
                          <div className="academic-delivery">
                            <code className="academic-mount-path">{asset.mount_path}</code>
                            <span className="secondary-line scientific-secondary">{asset.delivery_mode}</span>
                            <span className="academic-not-embedded">
                              {asset.embed_in_image === false ? "Not embedded in image" : "Embedded in image"}
                            </span>
                          </div>
                        </td>
                        <td>
                          <span className="academic-digest-line">
                            Artifact{" "}
                            {asset.artifact_sha256
                              ? <code title={asset.artifact_sha256}>{shortDigest(asset.artifact_sha256)}</code>
                              : "not verified"}
                          </span>
                          <span className="academic-digest-line">
                            Runtime image{" "}
                            {asset.runtime_image_digest
                              ? <code title={asset.runtime_image_digest}>{shortDigest(asset.runtime_image_digest)}</code>
                              : "not pinned"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          </div>
        )}
      </DataBoundary>
    </div>
  );
}
