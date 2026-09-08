import { useQuery } from "@tanstack/react-query";
import {
  NavLink,
  Outlet,
  useLocation,
  useSearchParams,
} from "react-router-dom";
import { adminApi, AdminApiError } from "../api/client";
import { formatTimestamp } from "../lib/format";
import { useSession } from "../auth/SessionContext";
import { useScientificCapabilities } from "../pages/scientific/useScientificCapabilities";
import {
  AdminTimeWindowControl,
  AdminTimeWindowProvider,
  useAdminTimeWindow,
} from "./AdminTimeWindow";
import nebiusLogo from "../assets/nebius-logo.svg";

const primaryNavigation = [
  ["Apps", "/admin/apps", "AP"],
  ["Users", "/admin/users", "US"],
  ["Capacity", "/admin/capacity", "CP"],
] as const;

const navigationBeforeScientific = [
  ["Platform overview", "/admin/overview", "OV"],
  ["Full model inventory", "/admin/model-inventory", "MI"],
  ["Models", "/admin/models", "MO"],
  ["Live model config", "/admin/model-deployments", "LC"],
  ["Operations", "/admin/operations", "OP"],
] as const;

const navigationAfterScientific = [
  ["Academic assets", "/admin/academic-assets", "AA"],
  ["Access administration", "/admin/access", "AK"],
  ["Capacity diagnostics", "/admin/advanced/capacity", "CQ"],
  ["Observability", "/admin/observability", "OB"],
  ["Configuration", "/admin/configuration", "CF"],
  ["Audit", "/admin/audit", "AU"],
] as const;

function titleFor(rawPathname: string, scientificLabel: string) {
  // The console is served at /admin/, so the overview arrives with a trailing
  // slash; normalise it before matching so the breadcrumb names the page.
  const pathname =
    rawPathname.length > 1 ? rawPathname.replace(/\/+$/, "") : rawPathname;
  if (pathname === "/admin") return "Apps";
  if (/^\/admin\/apps\/[^/]+/.test(pathname)) return "App";
  if (/^\/admin\/users\/[^/]+/.test(pathname)) return "User";
  if (pathname === "/admin/model-deployments/new")
    return "Draft model deployment";
  if (/^\/admin\/model-deployments\/[^/]+/.test(pathname))
    return "Model deployment";
  if (/^\/admin\/models\/[^/]+/.test(pathname)) return "Model detail";
  if (/^\/admin\/operations\/[^/]+/.test(pathname)) return "Operation detail";
  if (/^\/admin\/scientific-runs\/[^/]+/.test(pathname))
    return "Scientific run detail";
  if (pathname === "/admin/scientific-runs") return scientificLabel;
  if (/^\/admin\/academic-assets/.test(pathname)) return "Academic assets";
  return (
    [
      ...primaryNavigation,
      ...navigationBeforeScientific,
      ...navigationAfterScientific,
    ].find(([, path]) => path === pathname)?.[0] ?? "Nebius Apps"
  );
}

export function AppShell() {
  return (
    <AdminTimeWindowProvider>
      <AppShellContent />
    </AdminTimeWindowProvider>
  );
}

function AppShellContent() {
  const { session, logout, loggingOut, logoutError } = useSession();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const { params: sharedContext, navigation: navigationContext } =
    useAdminTimeWindow();
  const sharedContextSearch = sharedContext.toString();
  const scientificCapabilitiesQuery = useScientificCapabilities(sharedContext);
  const scientificCapabilities = scientificCapabilitiesQuery.data?.data;
  const showScientific =
    scientificCapabilities?.model_readiness.available === true ||
    scientificCapabilities?.run_history.available === true;
  const scientificLabel =
    scientificCapabilities?.run_history.available === true
      ? "Scientific runs"
      : "Scientific models";
  const navigation = [
    ...navigationBeforeScientific,
    ...(showScientific
      ? ([[scientificLabel, "/admin/scientific-runs", "SR"]] as const)
      : []),
    ...navigationAfterScientific,
  ];
  const contextQuery = useQuery({
    queryKey: ["admin-context", sharedContextSearch],
    queryFn: ({ signal }) => adminApi.context(sharedContext, signal),
  });
  const options = contextQuery.data?.data.options ?? [];
  const selected = contextQuery.data?.data.selected;
  const contextImpaired =
    contextQuery.data?.meta.sources.some(
      (source) => source.state !== "available",
    ) ?? false;
  const contextStatus = contextQuery.isPending
    ? "Checking"
    : contextQuery.isError || !contextQuery.data
      ? "Unavailable"
      : contextImpaired
        ? "Partial"
        : "Live";
  const contextError =
    contextQuery.error instanceof AdminApiError ? contextQuery.error : null;

  function changeContext(index: string) {
    const option = options[Number(index)];
    if (!option) return;
    const next = new URLSearchParams(searchParams);
    next.set("project", option.project);
    next.set("cluster", option.cluster);
    next.set("region", option.region);
    setSearchParams(next, { replace: true });
  }

  const selectedIndex = Math.max(
    0,
    options.findIndex(
      (option) =>
        option.project === selected?.project &&
        option.cluster === selected?.cluster &&
        option.region === selected?.region,
    ),
  );

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <aside className="product-rail" aria-label="Product navigation">
        <div className="wordmark">
          <img className="nebius-logo" src={nebiusLogo} alt="Nebius" />
          <span className="wordmark__product">Apps</span>
        </div>
        <nav aria-label="Main">
          {primaryNavigation.map(([label, path, short]) => (
            <NavLink
              key={path}
              to={{ pathname: path, search: navigationContext.toString() }}
            >
              <span className="nav-short" aria-hidden="true">
                {short}
              </span>
              <span className="nav-label">{label}</span>
            </NavLink>
          ))}
        </nav>
        <details
          className="advanced-navigation"
          open={
            !location.pathname.startsWith("/admin/apps") &&
            !location.pathname.startsWith("/admin/users") &&
            location.pathname !== "/admin/capacity" &&
            location.pathname !== "/admin"
          }
        >
          <summary>Advanced</summary>
          <nav aria-label="Advanced">
            {navigation.map(([label, path, short]) => (
              <NavLink
                key={path}
                to={{ pathname: path, search: navigationContext.toString() }}
              >
                <span className="nav-short" aria-hidden="true">
                  {short}
                </span>
                <span className="nav-label">{label}</span>
              </NavLink>
            ))}
          </nav>
        </details>
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
              {loggingOut
                ? "Signing out…"
                : logoutError
                  ? "Sign out failed · Retry"
                  : `${session.principal.role} · Sign out`}
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
            {options.length ? (
              options.map((option, index) => (
                <option
                  key={`${option.project}/${option.cluster}/${option.region}`}
                  value={index}
                >
                  {option.label}
                </option>
              ))
            ) : (
              <option>Context unavailable</option>
            )}
          </select>
        </label>
        <div className="context-divider" />
        <AdminTimeWindowControl />
        <span className="timezone">{selected?.timezone ?? "UTC"}</span>
        <span className="context-spacer" />
        <span className="generated-at">
          Cluster context checked{" "}
          {formatTimestamp(contextQuery.data?.meta.generated_at ?? null)}
        </span>
      </header>

      <main id="main-content" className="main-content" tabIndex={-1}>
        <div className="page-heading">
          <div>
            <span className="breadcrumb">
              Nebius / {titleFor(location.pathname, scientificLabel)}
            </span>
            <h1>{titleFor(location.pathname, scientificLabel)}</h1>
          </div>
          <div className="page-heading__context">
            {selected?.region ? (
              <span className="quiet-chip">{selected.region}</span>
            ) : null}
            <span
              className={`quiet-chip ${contextStatus === "Live" ? "quiet-chip--healthy" : "quiet-chip--warning"}`}
            >
              {contextStatus}
            </span>
          </div>
        </div>
        {contextQuery.isError ? (
          <div
            className="inline-notice inline-notice--error context-error"
            role="alert"
          >
            <strong>Cluster context is unavailable.</strong>{" "}
            {contextError?.message ??
              "The admin service did not return an authorized cluster context."}
            {contextError?.requestId ? (
              <code> Request {contextError.requestId}</code>
            ) : null}
            <button
              className="button"
              disabled={contextQuery.isFetching}
              onClick={() => void contextQuery.refetch()}
              type="button"
            >
              {contextQuery.isFetching ? "Retrying…" : "Try again"}
            </button>
          </div>
        ) : contextQuery.data && options.length === 0 ? (
          <div
            className="inline-notice inline-notice--warning context-error"
            role="status"
          >
            <strong>No authorized cluster context is configured.</strong> Model
            and capacity views may be unavailable until the backend publishes
            one.
          </div>
        ) : null}
        <Outlet />
      </main>
    </div>
  );
}
