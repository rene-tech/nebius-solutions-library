import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useSearchParams } from "react-router-dom";
import { sharedContextParams } from "../lib/search";

export const ADMIN_WINDOWS = {
  "1": "Last hour",
  "6": "Last 6 hours",
  "24": "Last 24 hours",
  "168": "Last 7 days",
};

export function resolveAdminWindow(search: URLSearchParams, now: number) {
  const requested = search.get("window");
  const explicit = Boolean(search.get("from") && search.get("to"));
  const range =
    requested && requested in ADMIN_WINDOWS
      ? requested
      : explicit
        ? "custom"
        : "1";
  const params = sharedContextParams(search);
  if (range !== "custom") {
    params.set("from", new Date(now - Number(range) * 3_600_000).toISOString());
    params.set("to", new Date(now).toISOString());
  }
  const navigation = new URLSearchParams(params);
  if (range !== "custom") navigation.set("window", range);
  return { params, navigation, range, live: range !== "custom" };
}

const WindowContext = createContext<ReturnType<
  typeof resolveAdminWindow
> | null>(null);

/** One clock shared by every tab/chart; saved absolute links stay absolute. */
export function AdminTimeWindowProvider({ children }: { children: ReactNode }) {
  const [search] = useSearchParams();
  const [now, setNow] = useState(Date.now);
  const key = search.toString();
  const value = useMemo(
    () => resolveAdminWindow(new URLSearchParams(key), now),
    [key, now],
  );
  useEffect(() => {
    if (!value.live) return;
    const timer = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(timer);
  }, [value.live]);
  return (
    <WindowContext.Provider value={value}>{children}</WindowContext.Provider>
  );
}

export function useAdminTimeWindow() {
  const context = useContext(WindowContext);
  const [search] = useSearchParams();
  // Legacy/test callers outside the shell still get a stable request window.
  const [now] = useState(Date.now);
  return context ?? resolveAdminWindow(search, now);
}

export function AdminTimeWindowControl() {
  const { range, params, live } = useAdminTimeWindow();
  const [search, setSearch] = useSearchParams();
  function change(value: string) {
    const next = new URLSearchParams(search);
    next.delete("cursor");
    if (value === "custom") {
      next.delete("window");
      next.set("from", params.get("from")!);
      next.set("to", params.get("to")!);
    } else {
      next.set("window", value);
      next.delete("from");
      next.delete("to");
    }
    setSearch(next);
  }
  return (
    <>
      <label>
        <span className="sr-only">Time range</span>
        <select
          aria-label="Time range"
          value={range}
          onChange={(event) => change(event.target.value)}
        >
          {Object.entries(ADMIN_WINDOWS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
          <option value="custom">Fixed time range</option>
        </select>
      </label>
      <span
        className="window-mode"
        title={`${params.get("from")} – ${params.get("to")}`}
      >
        {live ? "Live · 15s" : "Fixed"}
      </span>
    </>
  );
}
