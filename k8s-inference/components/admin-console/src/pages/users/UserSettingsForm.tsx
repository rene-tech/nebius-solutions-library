import { useState, type FormEvent } from "react";
import type {
  InferenceUser,
  UserAppChoice,
  UserCreate,
  UserPatch,
} from "../../api/userTypes";
import { Modal } from "../../components/Modal";

export function UserSettingsForm({
  user,
  apps = [],
  fixedTenant,
  busy,
  error,
  onClose,
  onSave,
}: {
  user?: InferenceUser;
  apps?: UserAppChoice[];
  fixedTenant?: string | null;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (payload: UserCreate | UserPatch) => Promise<void>;
}) {
  const [name, setName] = useState(user?.display_name ?? "");
  const [principal, setPrincipal] = useState("");
  const [tenant, setTenant] = useState(fixedTenant ?? "");
  const [kind, setKind] = useState(user?.kind ?? (user ? "unknown" : "human"));
  const [team, setTeam] = useState(user?.team ?? "");
  const [enabled, setEnabled] = useState(user?.enabled ?? true);
  const [academic, setAcademic] = useState(
    user?.academic_eligible === null || !user
      ? "inherit"
      : String(user.academic_eligible),
  );
  const [inheritApps, setInheritApps] = useState(user?.app_ids == null);
  const [selected, setSelected] = useState(new Set(user?.app_ids ?? []));
  async function submit(event: FormEvent) {
    event.preventDefault();
    const settings: UserPatch = {
      display_name: name.trim(),
      kind: kind === "unknown" ? null : (kind as "human" | "service"),
      team: team.trim() || null,
      enabled,
      academic_eligible: academic === "inherit" ? null : academic === "true",
      app_ids: inheritApps ? null : [...selected],
    };
    await onSave(
      user
        ? settings
        : ({
            ...settings,
            tenant_id: tenant.trim(),
            principal_id: principal.trim(),
          } as UserCreate),
    );
  }
  return (
    <Modal
      title={user ? "User settings" : "Create inference user"}
      description="Inference ownership and access are separate from console operator roles."
      onClose={onClose}
    >
      <form className="form-grid" onSubmit={(event) => void submit(event)}>
        <label>
          Display name
          <input
            required
            maxLength={160}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        {!user && (
          <>
            <label>
              Owner identity
              <input
                required
                maxLength={160}
                value={principal}
                onChange={(event) => setPrincipal(event.target.value)}
              />
            </label>
            <label>
              Tenant
              <input
                required
                disabled={Boolean(fixedTenant)}
                maxLength={120}
                value={tenant}
                onChange={(event) => setTenant(event.target.value)}
              />
            </label>
          </>
        )}
        <label>
          Identity type
          <select
            value={kind}
            onChange={(event) => setKind(event.target.value)}
          >
            {user && <option value="unknown">Not classified</option>}
            <option value="human">Person</option>
            <option value="service">Service account</option>
          </select>
        </label>
        <label>
          Team
          <input
            maxLength={160}
            value={team}
            onChange={(event) => setTeam(event.target.value)}
          />
        </label>
        <label>
          Academic affiliation (informational)
          <select
            value={academic}
            onChange={(event) => setAcademic(event.target.value)}
          >
            <option value="inherit">Not specified</option>
            <option value="true">Academic</option>
            <option value="false">Non-academic</option>
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
          />
          Allow new inference requests
        </label>
        <fieldset className="form-grid__wide">
          <legend>Allowed apps</legend>
          <label>
            <input
              type="checkbox"
              checked={inheritApps}
              onChange={(event) => setInheritApps(event.target.checked)}
            />
            Use existing API key app permissions
          </label>
          {!inheritApps &&
            (apps.length ? (
              apps.map((app) => (
                <label key={app.app_id}>
                  <input
                    type="checkbox"
                    checked={selected.has(app.app_id)}
                    onChange={(event) =>
                      setSelected((before) => {
                        const next = new Set(before);
                        if (event.target.checked) next.add(app.app_id);
                        else next.delete(app.app_id);
                        return next;
                      })
                    }
                  />
                  {app.display_name} · {app.public_model_id}
                  {app.academic_required ? " · academic" : ""}
                </label>
              ))
            ) : (
              <p>
                No apps available. An empty selection denies all new app
                invocations.
              </p>
            ))}
        </fieldset>
        <p className="form-grid__wide">
          User settings restrict each key; they never add permissions missing
          from the key. All models, including academic models, use these same
          app permissions; academic affiliation does not grant or deny access.
          Disabling stops new work but preserves access to
          existing operation results.
        </p>
        {error && (
          <p role="alert" className="form-grid__wide">
            {error}
          </p>
        )}
        <div className="modal-actions form-grid__wide">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button button--primary"
            disabled={busy}
          >
            {busy ? "Saving…" : "Save user"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
