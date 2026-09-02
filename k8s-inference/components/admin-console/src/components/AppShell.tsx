import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useLocation, useSearchParams } from "react-router-dom";
import { adminApi, AdminApiError } from "../api/client";
import { formatTimestamp } from "../lib/format";
import { sharedContextParams } from "../lib/search";
import { useSession } from "../auth/SessionContext";

const navigation = [
  ["Overview", "/admin", "OV"],
  ["Models", "/admin/models", "MO"],
  ["Live model config", "/admin/model-deployments", "LC"],
  ["Operations", "/admin/operations", "OP"],
  ["Scientific runs", "/admin/scientific-runs", "SR"],
  ["Users & API keys", "/admin/access", "AK"],
  ["Capacity & queues", "/admin/capacity", "CQ"],
  ["Observability", "/admin/observability", "OB"],
  ["Configuration", "/admin/configuration", "CF"],
  ["Audit", "/admin/audit", "AU"],
] as const;

function titleFor(pathname: string) {
  if (pathname === "/admin/model-deployments/new") return "Draft model deployment";
  if (/^\/admin\/model-deployments\/[^/]+/.test(pathname)) return "Model deployment";
  if (/^\/admin\/models\/[^/]+/.test(pathname)) return "Model detail";
  if (/^\/admin\/operations\/[^/]+/.test(pathname)) return "Operation detail";
  if (/^\/admin\/scientific-runs\/[^/]+/.test(pathname)) return "Scientific run detail";
  return navigation.find(([, path]) => path === pathname)?.[0] ?? "FS2 Serve";
}

export function AppShell() {
  const { session, logout, loggingOut, logoutError } = useSession();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const sharedContext = sharedContextParams(searchParams);
  const sharedContextSearch = sharedContext.toString();
  const contextQuery = useQuery({
    queryKey: ["admin-context", sharedContextSearch],
    queryFn: ({ signal }) => adminApi.context(sharedContext, signal),
  });
  const options = contextQuery.data?.data.options ?? [];
  const selected = contextQuery.data?.data.selected;
  const contextImpaired = contextQuery.data?.meta.sources.some((source) => source.state !== "available") ?? false;
  const contextStatus = contextQuery.isPending
    ? "Checking"
    : contextQuery.isError || !contextQuery.data
      ? "Unavailable"
      : contextImpaired
        ? "Partial"
        : "Live";
  const contextError = contextQuery.error instanceof AdminApiError ? contextQuery.error : null;

  function changeContext(index: string) {
    const option = options[Number(index)];
    if (!option) return;
    const next = new URLSearchParams(searchParams);
    next.set("project", option.project);
    next.set("cluster", option.cluster);
    next.set("region", option.region);
    setSearchParams(next, { replace: true });
  }

  function changeWindow(hours: string) {
    const next = new URLSearchParams(searchParams);
    const to = new Date();
    const from = new Date(to.valueOf() - Number(hours) * 3_600_000);
    next.set("from", from.toISOString());
    next.set("to", to.toISOString());
    setSearchParams(next);
  }

  const selectedIndex = Math.max(
    0,
    options.findIndex(
      (option) => option.project === selected?.project && option.cluster === selected?.cluster && option.region === selected?.region,
    ),
  );

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <aside className="product-rail" aria-label="Product navigation">
        <div className="wordmark">
          <span className="wordmark__mark" aria-hidden="true">F2</span>
          <span className="wordmark__text">FS2 Serve</span>
        </div>
        <nav>
          {navigation.map(([label, path, short]) => (
            <NavLink key={path} to={{ pathname: path, search: sharedContextSearch ? `?${sharedContextSearch}` : "" }} end={path === "/admin"}>
              <span className="nav-short" aria-hidden="true">{short}</span>
              <span className="nav-label">{label}</span>
            </NavLink>
          ))}
        </nav>
        <button
          aria-label={`Sign out ${session.principal.display_name}`}
          className="rail-footer rail-footer--button"
          disabled={loggingOut}
          onClick={() => void logout()}
          type="button"
          title={logoutError?.message}
        >
          <span className="operator-avatar" aria-hidden="true">
            {session.principal.display_name.slice(0, 2).toUpperCase()}
          </span>
          <span className="nav-label">
            {session.principal.display_name}
            <small aria-live="polite">
              {loggingOut ? "Signing out…" : logoutError ? "Sign out failed · Retry" : `${session.principal.role} · Sign out`}
            </small>
          </span>
        </button>
      </aside>

      <header className="context-bar">
        <label>
          <span className="sr-only">Project, cluster and region</span>
          <select
            aria-label="Project, cluster and region"
            disabled={contextQuery.isPending || options.length === 0}
            value={selectedIndex}
            onChange={(event) => changeContext(event.target.value)}
          >
            {options.length ? options.map((option, index) => <option key={`${option.project}/${option.cluster}/${option.region}`} value={index}>{option.label}</option>) : <option>Context unavailable</option>}
          </select>
        </label>
        <div className="context-divider" />
        <label>
          <span className="sr-only">Time range</span>
          <select aria-label="Time range" defaultValue="1" onChange={(event) => changeWindow(event.target.value)}>
            <option value="1">Last hour</option>
            <option value="6">Last 6 hours</option>
            <option value="24">Last 24 hours</option>
          </select>
        </label>
        <span className="timezone">{selected?.timezone ?? "UTC"}</span>
        <span className="context-spacer" />
        <span className="generated-at">Updated {formatTimestamp(contextQuery.data?.meta.generated_at ?? null)}</span>
      </header>

      <main id="main-content" className="main-content" tabIndex={-1}>
        <div className="page-heading">
          <div>
            <span className="breadcrumb">FS2 Serve / {titleFor(location.pathname)}</span>
            <h1>{titleFor(location.pathname)}</h1>
          </div>
          <div className="page-heading__context">
            {selected?.region ? <span className="quiet-chip">{selected.region}</span> : null}
            <span className={`quiet-chip ${contextStatus === "Live" ? "quiet-chip--healthy" : "quiet-chip--warning"}`}>{contextStatus}</span>
          </div>
        </div>
        {contextQuery.isError ? (
          <div className="inline-notice inline-notice--error context-error" role="alert">
            <strong>Cluster context is unavailable.</strong> {contextError?.message ?? "The admin service did not return an authorized cluster context."}
            {contextError?.requestId ? <code> Request {contextError.requestId}</code> : null}
            <button className="button" disabled={contextQuery.isFetching} onClick={() => void contextQuery.refetch()} type="button">{contextQuery.isFetching ? "Retrying…" : "Try again"}</button>
          </div>
        ) : contextQuery.data && options.length === 0 ? (
          <div className="inline-notice inline-notice--warning context-error" role="status"><strong>No authorized cluster context is configured.</strong> Model and capacity views may be unavailable until the backend publishes one.</div>
        ) : null}
        <Outlet />
      </main>
    </div>
  );
}
