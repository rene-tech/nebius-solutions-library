"""The list-watch store: one in-memory copy of the fleet's Jobs, pods, JobSets and Workloads, kept current by the
Kubernetes watch API (the informer pattern of client-go), read by every listing this API serves.

One reflector thread per (region, namespace, kind): LIST once (the collection's `resourceVersion`), then WATCH from
that version with bookmarks; ADDED/MODIFIED replace one object in place, DELETED removes it, a watch that ends (the
server's timeout) is reopened from the last version, 410 Gone or any other failure relists after a short backoff. A
reader takes the current objects (shared, read-only by contract: a reader that fills a view in copies the object)
in O(1) per object: no listing per request, no warmer, no generations to fence, one copy of each object per process
instead of one per 4 s plus one per view (the 2026-10-20 OOM rounds, api 0.11.138-158). A (region, namespace, kind)
that is not synced yet, or whose watch has been broken for STORE_STALE_S, says so (`ready` False) and the reader
lists directly, so startup, a region outage and the tests behave as before.
"""

import json
import logging
import threading
import time

from kubernetes.client.rest import ApiException
from kubernetes.watch.watch import iter_resp_lines

import kube
from config import (
    JOBSET_GROUP,
    JOBSET_PLURAL,
    JOBSET_VERSION,
    LABEL,
    REGION,
    STORE_NS_REFRESH_S,
    STORE_RESYNC_S,
    STORE_STALE_S,
    STORE_WATCH_TIMEOUT_S,
)

log = logging.getLogger("api")

Key = tuple[str, str, str]  # (region, namespace, kind)


def _jobs(r, ns, **kw):
    return kube.batch(r).list_namespaced_job(ns, **kw)


def _pods(r, ns, **kw):
    return kube.core(r).list_namespaced_pod(ns, **kw)


def _jobsets(r, ns, **kw):
    return kube.api(r).list_namespaced_custom_object(JOBSET_GROUP, JOBSET_VERSION, ns, JOBSET_PLURAL, **kw)


def _workloads(r, ns, **kw):
    return kube.api(r).list_namespaced_custom_object("kueue.x-k8s.io", "v1beta2", ns, "workloads", **kw)


def _plain(obj: dict) -> dict:
    """The object as the API server serves it, without the field-ownership ledger (metadata.managedFields: 47% of a
    Job's bytes, read by nothing here) and without a reader's private `_` fields."""
    obj = {k: v for k, v in obj.items() if not k.startswith("_")}
    (obj.get("metadata") or {}).pop("managedFields", None)
    return obj


# kind -> (lister, label selector, regions (None: every region), compaction of a stored object)
KINDS: dict = {
    "jobs": (_jobs, f"{LABEL}/tenant", None, _plain),
    "pods": (_pods, f"{LABEL}/tenant", None, _plain),
    "jobsets": (_jobsets, f"{LABEL}/tenant", None, _plain),
    "workloads": (_workloads, None, (REGION,), lambda w: kube._compact_workload(_plain(w))),
}

_lock = threading.Lock()
_items: dict[Key, dict[str, dict]] = {}  # key -> name -> object
_state: dict[Key, dict] = {}  # key -> synced, rv, version, listed_at, error, error_since
_threads: dict[Key, threading.Thread] = {}
_absent: dict[Key, float] = {}  # keys whose namespace answered 403/404: not retried before this time
_stop = threading.Event()


def key(region: str, ns: str, kind: str) -> Key:
    return (region, ns, kind)


def _st(k: Key) -> dict:
    return _state.setdefault(
        k, {"synced": False, "rv": None, "version": 0, "listed_at": 0.0, "error": None, "error_since": None}
    )


def ready(region: str, ns: str, kind: str) -> bool:
    """Synced, and its watch not broken for longer than STORE_STALE_S."""
    with _lock:
        st = _state.get((region, ns, kind))
        if not st or not st["synced"]:
            return False
        return st["error_since"] is None or time.time() - st["error_since"] < STORE_STALE_S


def items(region: str, ns: str, kind: str) -> list[dict]:
    """The current objects (shared, read-only)."""
    with _lock:
        return list(_items.get((region, ns, kind), {}).values())


def get(region: str, ns: str, kind: str, name: str) -> dict | None:
    with _lock:
        return _items.get((region, ns, kind), {}).get(name)


def version(region: str, ns: str, kind: str) -> int:
    """A monotonic counter of changes to this key: what a reader's own cache fences on."""
    with _lock:
        return _st((region, ns, kind))["version"]


def replace(k: Key, objects: list[dict], rv: str | None) -> None:
    """The LIST: the whole collection at `rv` replaces what was held."""
    compact = KINDS[k[2]][3] if k[2] in KINDS else _plain
    fresh = {o["metadata"]["name"]: compact(o) for o in objects}
    with _lock:
        _items[k] = fresh
        st = _st(k)
        st.update(synced=True, rv=rv, listed_at=time.time(), error=None, error_since=None)
        st["version"] += 1


def apply(k: Key, event: str, obj: dict) -> None:
    """One watch event (ADDED, MODIFIED, DELETED); BOOKMARK moves the version only."""
    rv = (obj.get("metadata") or {}).get("resourceVersion")
    if event == "BOOKMARK":
        with _lock:
            _st(k)["rv"] = rv
        return
    name = obj["metadata"]["name"]
    compact = KINDS[k[2]][3] if k[2] in KINDS else _plain
    with _lock:
        held = _items.setdefault(k, {})
        if event == "DELETED":
            held.pop(name, None)
        else:
            held[name] = compact(obj)
        st = _st(k)
        st["rv"] = rv or st["rv"]
        st["version"] += 1


def observe(region: str, ns: str, kind: str, obj: dict) -> None:
    """A mutation's response (create, patch): applied if newer than what is held, so this replica's own writes show
    on its next listing before the watch delivers them."""
    name = obj["metadata"]["name"]
    with _lock:
        held = _items.get((region, ns, kind), {}).get(name)
    try:
        if held and int(held["metadata"].get("resourceVersion") or 0) >= int(obj["metadata"].get("resourceVersion")):
            return
    except (TypeError, ValueError):
        pass
    apply((region, ns, kind), "MODIFIED", obj)


def drop(region: str, ns: str, kind: str, name: str) -> None:
    """A deletion this replica made: gone from the store now (the watch's DELETED is idempotent)."""
    apply((region, ns, kind), "DELETED", {"metadata": {"name": name}})


def forget(k: Key) -> None:
    with _lock:
        _items.pop(k, None)
        _state.pop(k, None)


def degrade(k: Key, error: str) -> None:
    with _lock:
        st = _st(k)
        st["error"] = error
        st["error_since"] = st["error_since"] or time.time()


class _Gone(Exception):
    """410: the watch's version is older than the server keeps; relist."""


def sync(k: Key, lister, selector: str | None) -> None:
    """One LIST, then one WATCH session from its version until the server ends it (STORE_WATCH_TIMEOUT_S) or
    STORE_RESYNC_S passed since the LIST; raises on 410 (_Gone) and on any failure (the caller backs off and relists).
    `lister(region, ns, **kw)` is the Kubernetes client's list function (raw JSON, `_preload_content=False`)."""
    region, ns, _kind = k
    sel = {"label_selector": selector} if selector else {}
    resp = lister(region, ns, _preload_content=False, **sel)
    data = json.loads(resp.data)
    rv = (data.get("metadata") or {}).get("resourceVersion")
    replace(k, data.get("items") or [], rv)
    listed = time.time()
    while not _stop.is_set() and time.time() - listed < STORE_RESYNC_S:
        resp = lister(
            region,
            ns,
            watch=True,
            resource_version=rv,
            allow_watch_bookmarks=True,
            timeout_seconds=int(STORE_WATCH_TIMEOUT_S),
            _preload_content=False,
            **sel,
        )
        for line in iter_resp_lines(resp):
            if not line:
                continue
            ev = json.loads(line)
            obj = ev.get("object") or {}
            if ev.get("type") == "ERROR":
                if obj.get("code") == 410:
                    raise _Gone(obj.get("message", "410"))
                raise RuntimeError(f"watch error: {obj.get('message') or obj}")
            apply(k, ev["type"], obj)
            rv = (obj.get("metadata") or {}).get("resourceVersion") or rv
            if _stop.is_set():
                return


def _run(k: Key) -> None:
    lister, selector, _regions, _compact = KINDS[k[2]]
    backoff = 1.0
    while not _stop.is_set():
        try:
            sync(k, lister, selector)
            backoff = 1.0
        except _Gone:
            continue
        except ApiException as e:
            if e.status in (403, 404):  # the namespace is not (yet, any more) onboarded in this region: ready and
                replace(k, [], None)  # empty, as a direct listing treats it; looked at again after a while
                with _lock:
                    _absent[k] = time.time() + STORE_NS_REFRESH_S * 5
                return
            degrade(k, f"{e.status} {e.reason}")
        except Exception as e:  # noqa: BLE001 - the reflector never dies; the store serves what it holds meanwhile
            degrade(k, f"{type(e).__name__}: {str(e)[:120]}")
        if _stop.wait(backoff):
            return
        backoff = min(backoff * 2, 30.0)


def _supervise(namespaces_fn) -> None:
    """Every STORE_NS_REFRESH_S: a reflector for every (region, tenant namespace, kind) that has none running."""
    while not _stop.is_set():
        try:
            regions = kube.regions()
            namespaces = namespaces_fn()
            now = time.time()
            for kind, (_l, _s, kind_regions, _c) in KINDS.items():
                for r in kind_regions or regions:
                    for ns in namespaces:
                        k = (r, ns, kind)
                        with _lock:
                            t = _threads.get(k)
                            if (t and t.is_alive()) or _absent.get(k, 0) > now:
                                continue
                            t = threading.Thread(target=_run, args=(k,), name=f"store-{r}-{ns}-{kind}", daemon=True)
                            _threads[k] = t
                        t.start()
        except Exception as e:  # noqa: BLE001 - the next pass tries again
            log.warning("store: supervisor pass failed: %s", str(e)[:160])
        if _stop.wait(STORE_NS_REFRESH_S):
            return


def start(namespaces_fn) -> None:
    """Start the store (the API's lifespan): `namespaces_fn()` names the tenant namespaces to watch."""
    _stop.clear()
    threading.Thread(target=_supervise, args=(namespaces_fn,), name="store-supervisor", daemon=True).start()


def stop() -> None:
    _stop.set()


def reset() -> None:
    """Forget everything (tests)."""
    with _lock:
        _items.clear()
        _state.clear()
        _absent.clear()


def health() -> dict:
    """Per key: synced, version, age of the last list, the current error (the fleet operator's view, /healthz)."""
    with _lock:
        now = time.time()
        return {
            "/".join(k): {
                "synced": st["synced"],
                "objects": len(_items.get(k, {})),
                "version": st["version"],
                "listed_s_ago": round(now - st["listed_at"]) if st["listed_at"] else None,
                "error": st["error"],
            }
            for k, st in sorted(_state.items())
        }
