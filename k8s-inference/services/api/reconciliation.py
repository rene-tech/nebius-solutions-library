"""Durable fleet reconciliation, independent of HTTP routing.

Desired model generations commit before reconciliation. The database lock serializes reconcilers;
finish_change fences completion by generation. Partial failures remain pending and retry idempotently.
Authorization policies stay until the corresponding predictor routes have disappeared.
"""

import asyncio
import json
import logging
import os
import re
import time
from contextlib import nullcontext
from datetime import datetime, timezone

from fastapi import HTTPException
from kubernetes.client.rest import ApiException

import attempts
import catalog
import config as cfg
import db
import execution
import jobs
import keys
import kube
import models
import status
from config import FLEET_MANAGER, REGION
from resilience import retry

log = logging.getLogger("api")
RESERVATION_STALE_S = 3600


def _deploy(entry: dict, after: dict, result: dict):
    """Render the endpoint on every cluster of its `deployments`, put its hostname on the region's certificate
    and register its LiteLLM group there. `after` is the runtime catalog as it will be after this write."""
    if entry["mode"] == "run":
        return
    for cid in entry["deployments"]:
        region = kube.cluster_region(cid)
        if region not in kube.regions():
            raise HTTPException(400, f"regions: no connection to {region} from this API (REGION_KUBECONFIGS)")
        result["applied"][cid] = models.apply(entry, cid, region)
        models.sync_certificate(region, after)
        if w := models.litellm_group_upsert(entry, cid, region):
            result.setdefault("warnings", []).append(w)
            raise RuntimeError(w)


def _undeploy(entry: dict, clusters: list[str], after: dict):
    if entry.get("mode") == "run":
        return
    for cid in clusters:
        region = kube.cluster_region(cid)
        models.delete_rendered(entry, cid, region)
        models.sync_certificate(region, after)
        if warning := models.litellm_group_delete(
            entry["id"], cid, [v["name"] for v in models.variants(entry, cid)[1:]]
        ):
            raise RuntimeError(warning)


def _prune_variants(before: dict, entry: dict, clusters: list[str]):
    """A redefinition that drops a variant while its region stays deployed: `apply` renders only the new variants, so
    the dropped one's InferenceService (its warm replicas hold GPUs), authorization and LiteLLM deployment are
    removed here, with the same route-teardown retention as a region's removal (models.delete_variants)."""
    for cid in clusters:
        objects, ids = models.removed_variants(before, entry, cid)
        if not objects:
            continue
        models.delete_variants(before, cid, objects, kube.cluster_region(cid))
        if warning := models.litellm_deployments_delete(ids):
            raise RuntimeError(warning)


def reconcile_models() -> dict:
    results = {}
    with db.reconciler() as acquired:
        if not acquired:
            return results
        for change in db.pending_changes():
            mid, entry, before = change["id"], change["entry"], change["previous"]
            result = {"applied": {}, "version": change["version"]}
            try:
                after = models.list_runtime()
                if entry:
                    _deploy(entry, after, result)
                removed = list(before.get("deployments", {}))
                if entry and entry.get("mode") != "run":
                    removed = [c for c in removed if c not in entry.get("deployments", {})]
                if before:
                    _undeploy(before, removed, after)
                    if entry and entry.get("mode") != "run" and before.get("mode") != "run":
                        _prune_variants(before, entry, [c for c in before.get("deployments", {}) if c not in removed])
                db.finish_change(mid, change["version"])
            except Exception:
                log.exception("model reconciliation failed for %s generation %s", mid, change["version"])
                result["pending"] = True
                db.finish_change(mid, change["version"], "Deployment reconciliation failed; retrying.")
            results[mid] = result
    return results


def idle_shutdown() -> list[str]:
    """Multi-node endpoints (`nodes` > 1) with scaling.min 1 and scaling.idle_s: stopped (scaling.min written to 0 in
    the fleet database, history entry "idle shutdown") once no request reached them for idle_s seconds, counted from
    the later of the last request (the regional API stamps the LeaderWorkerSet on every authorized call) and the
    moment the leader became Ready. Returns the ids stopped. Control API only."""
    if not db.enabled():
        return []
    stopped = []
    now = datetime.now(timezone.utc)
    for m in list(catalog.all_models().values()):
        spec = m.get("spec") or {}
        sc = spec.get("scaling") or {}
        if (
            m.get("managed_by") != "api"
            or int(m.get("nodes") or 1) <= 1
            or int(sc.get("min") or 0) < 1
            or not sc.get("idle_s")
        ):
            continue
        try:
            import sessions

            busy = sessions.open_on(m["name"]) > 0  # a live session stamps nothing: it is activity nonetheless
            for cid in m.get("deployments") or {}:
                if busy:
                    break
                region = kube.cluster_region(cid)
                st = kube.endpoint_status(m["k8s_name"], m["namespace"], region, nodes=int(m["nodes"]))
                marks = [st.get(k) for k in ("ready_at", "last_request_at") if st.get(k)]
                if st["status"] != "ready" or not marks:
                    busy = True  # starting, or no Ready pod yet: nothing to measure
                    continue
                last = max(datetime.fromisoformat(x.replace("Z", "+00:00")) for x in marks)
                if (now - last).total_seconds() < int(sc["idle_s"]):
                    busy = True
            if busy:
                continue
            new_spec = {**spec, "scaling": {**sc, "min": 0}}
            entry = models.to_entry(new_spec, managed_by="api")
            row = db.get_model(m["name"])
            try:  # an admin's write in between wins: this round skips, the next one re-reads
                db.upsert_model(
                    entry, new_spec, by="idle-shutdown", expected=row.get("version") if row else None, create_only=False
                )
            except HTTPException as e:
                if e.status_code != 409:
                    raise
                continue
            catalog.invalidate()
            stopped.append(m["name"])
            log.info("idle shutdown: %s stopped after %ss without a request", m["name"], sc["idle_s"])
        except Exception:
            log.exception("idle shutdown check failed for %s", m["name"])
    return stopped


def continue_run(root: str, build, transfer_only: bool = False, ns: str | None = None):
    from app import continue_run as _continue_run  # app imports this module; resolved at call time

    return _continue_run(root, build, transfer_only, ns=ns)


def settle_deleted(job: dict) -> None:
    from app import settle_deleted as _settle_deleted

    _settle_deleted(job)


def _run_jobs(ns: str) -> list[dict]:
    """The tenant's run Jobs as the API server's JSON items (jobs._json_items: raw response, managedFields dropped), the
    shape sanitize_for_serialization produced before. The three switches each built 999 typed V1Job objects and their
    dict copies every 15 s (api 0.11.155; the replicas climbed ~400 MiB/min on that, 2026-10-21)."""
    return jobs._json_items(kube.batch().list_namespaced_job, ns, label_selector=f"{cfg.LABEL}/mode=run")


def fallback_switch() -> list[str]:
    """Fleet runs that asked for `placement.fallback_wait_s` and are still unplaced after it: moved to the next class of
    their preference (jobs.switch_class). Returns the new operation ids. Control API only."""
    if not FLEET_MANAGER:
        return []
    moved = []
    now = datetime.now(timezone.utc)
    try:
        namespaces = [x.metadata.name for x in kube.core().list_namespace(label_selector=f"{cfg.LABEL}/tenant").items]
    except Exception:  # noqa: BLE001
        return []
    for ns in namespaces:
        try:
            items = _run_jobs(ns)
        except Exception:  # noqa: BLE001
            continue
        clusters = None
        for job in items:
            ann = job["metadata"].get("annotations") or {}
            wait = ann.get(f"{cfg.LABEL}/fallback-wait-s")
            if (
                wait is None
                or not jobs.is_manager_job(job)
                or status.finished(job)
                or ann.get(f"{cfg.LABEL}/successor")
            ):
                continue
            created = datetime.fromisoformat(str(job["metadata"]["creationTimestamp"]).replace("Z", "+00:00"))
            if (now - created).total_seconds() < int(wait):
                continue
            if clusters is None:
                clusters = kube.workload_clusters(ns)
            if clusters.get(job["metadata"].get("uid", "")):
                continue  # placed: the wait is over
            try:
                ex = jobs.normalise(job).get("execution") or {}
                pref = (ex.get("placement") or {}).get("gpu_preference") or []
                current = job["metadata"].get("labels", {}).get(f"{cfg.LABEL}/gpu-class") or (pref[0] if pref else None)
                remaining = pref[pref.index(current) + 1 :] if current in pref else []
                if not remaining:
                    continue  # the last class of the preference: keep waiting
                m = catalog.require(job["metadata"]["labels"].get(f"{cfg.LABEL}/model", ""))
                root = ann.get(f"{cfg.LABEL}/resumed-from") or job["metadata"]["name"]
                new, _ = continue_run(
                    root,
                    lambda admit: jobs.switch_class(
                        ns,
                        job["metadata"]["name"],
                        REGION,
                        m,
                        catalog.fleet_params(m),
                        remaining[0],
                        remaining,
                        admit=admit,
                    ),
                    ns=ns,
                )
                moved.append(new["metadata"]["name"])
                log.info(
                    "fallback: %s/%s -> %s on %s", ns, job["metadata"]["name"], new["metadata"]["name"], remaining[0]
                )
            except Exception:  # noqa: BLE001
                log.exception("fallback of %s/%s failed", ns, job["metadata"]["name"])
    return moved


UNSCHEDULABLE_WAIT_S = int(os.environ.get("UNSCHEDULABLE_WAIT_S", "600"))  # how long a pod must sit unschedulable
UNSCHEDULABLE_MAX_SWITCHES = 2  # per root: a shape that keeps landing where it cannot fit is an operator's call
UNSCHEDULABLE_REASON = "Unschedulable"


def _unschedulable_since(pod: dict) -> datetime | None:
    """When the scheduler last said the pod cannot be placed (PodScheduled=False, reason Unschedulable), None otherwise."""
    for c in (pod.get("status") or {}).get("conditions") or []:
        if c.get("type") == "PodScheduled" and c.get("status") == "False" and c.get("reason") == UNSCHEDULABLE_REASON:
            t = c.get("lastTransitionTime")
            return datetime.fromisoformat(str(t).replace("Z", "+00:00")) if t else None
    return None


def unschedulable_switch() -> list[str]:
    """Admitted runs whose pod can never schedule where Kueue put it (docs/SCHEDULING.md "Per-node fit"): every pod of
    the run is Pending and unbound, the newest has been `Unschedulable` for UNSCHEDULABLE_WAIT_S, and the pool the
    worker's Kueue assigned (the pod's injected nodeSelector) has no node that holds the pod's requests (fit.yaml). Such a
    run is moved to a successor on the same root rendered with the fit affinity (jobs.switch_unschedulable), at most
    UNSCHEDULABLE_MAX_SWITCHES times per root. A pod that merely waits for a node of a pool that CAN hold it (a pool
    scaling from zero, a full pool: temporarily unavailable) is left alone; a shape no pool anywhere holds is an
    impossible shape: annotated once (`unschedulable`), never moved, the operator decides. Control API only."""
    import fit

    if not FLEET_MANAGER or not fit.table():
        return []
    moved = []
    now = datetime.now(timezone.utc)
    try:
        namespaces = [x.metadata.name for x in kube.core().list_namespace(label_selector=f"{cfg.LABEL}/tenant").items]
    except Exception:  # noqa: BLE001
        return []
    for ns in namespaces:
        try:
            items = _run_jobs(ns)
        except Exception:  # noqa: BLE001
            continue
        clusters = None
        for item in items:
            md = item["metadata"]
            ann = md.get("annotations") or {}
            name = md["name"]
            if (
                not jobs.is_manager_job(item)
                or status.finished(item)
                or ann.get(f"{cfg.LABEL}/successor")
                or ann.get(f"{cfg.LABEL}/cancelled")
                or ann.get(f"{cfg.LABEL}/unschedulable")
                or (item.get("spec") or {}).get("suspend")
            ):
                continue
            if clusters is None:
                clusters = kube.workload_clusters(ns)
            if not clusters.get(md.get("uid", "")):
                continue  # not placed: Kueue has not admitted it anywhere (that is the queue, not this)
            try:
                job = jobs.get(ns, name, REGION)
                pods = job.get("_pods") or []
                if not pods or job.get("_pods_unknown"):
                    continue
                if any(
                    (p.get("spec") or {}).get("nodeName") or (p.get("status") or {}).get("phase") != "Pending"
                    for p in pods
                ):
                    continue  # an attempt started (or ended): never an unschedulable switch
                newest = pods[-1]
                since = _unschedulable_since(newest)
                if since is None or (now - since).total_seconds() < UNSCHEDULABLE_WAIT_S:
                    continue
                holds = fit.assigned_pool_holds(newest, job.get("_cluster"))
                if holds is None or holds:
                    continue  # unknown pool/table (no judgement) or a pool that can hold it: waiting for a node
                req = fit.pod_requests(newest.get("spec") or {})
                anywhere = fit.fitting(req)
                pool = ((newest.get("spec") or {}).get("nodeSelector") or {}).get(f"{cfg.LABEL}/pool")
                cluster = job.get("_cluster")
                if not anywhere:
                    _annotate(
                        ns,
                        name,
                        {
                            f"{cfg.LABEL}/unschedulable": f"impossible shape: the pod needs {fit.describe(req)} on one node; "
                            f"no pool of the fleet holds it (assigned {cluster}/{pool}); the largest node admits {fit.largest()}"
                        },
                    )
                    log.warning("unschedulable %s/%s: impossible shape, left to the operator", ns, name)
                    continue
                root = ann.get(f"{cfg.LABEL}/resumed-from") or name
                done = sum(
                    1
                    for j in items
                    if (j["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") == root
                    and str((j["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/recovery") or "").startswith(
                        "unschedulable"
                    )
                )
                if done >= UNSCHEDULABLE_MAX_SWITCHES:
                    _annotate(
                        ns,
                        name,
                        {
                            f"{cfg.LABEL}/unschedulable": f"{done} unschedulable switches on this root already; left to the operator"
                        },
                    )
                    continue
                m = catalog.require(md["labels"].get(f"{cfg.LABEL}/model", ""))
                why = (
                    f"unschedulable on {cluster}/{pool} for {int((now - since).total_seconds())} s: the pod needs "
                    f"{fit.describe(req)} on one node and no node of that pool holds it; re-placed on the pools whose node does "
                    f"({', '.join(sorted(anywhere))})"
                )
                new, _ = continue_run(
                    root,
                    lambda admit: jobs.switch_unschedulable(
                        ns, name, REGION, m, catalog.fleet_params(m), why, admit=admit
                    ),
                    ns=ns,
                )
                moved.append(new["metadata"]["name"])
                log.info("unschedulable switch: %s/%s -> %s (%s)", ns, name, new["metadata"]["name"], why)
            except Exception:  # noqa: BLE001
                log.exception("unschedulable switch of %s/%s failed", ns, name)
    return moved


def _lost(op: dict, job: dict) -> tuple[dict | None, str | None]:
    """The confirmed infrastructure loss of a run, if any: (the lost attempt, why) when no attempt is RUNNING and the
    newest attempt that ran ended PREEMPTED (a pod with DisruptionTarget or deleted, an uploader record `interrupted`,
    or a `started` record whose pod no longer exists on the worker). Never: an application failure (exit code, OOM),
    a deadline, a cancel, a finished run, a run that never started, or a worker whose pods are unknown."""
    if job.get("_pods_unknown") or op["status"] not in ("QUEUED", "RUNNING"):
        return None, None
    attempts_ = op.get("attempts") or []
    if any(a["status"] == "RUNNING" for a in attempts_):
        return None, None
    ran = [a for a in attempts_ if a.get("started_at")]
    if not ran or ran[-1]["status"] != "PREEMPTED":
        return None, None
    return ran[-1], ran[-1].get("reason") or "infrastructure loss"


def recovery_switch() -> list[str]:
    """Runs that asked for `recovery.on_infrastructure_loss: recover` and lost their worker (docs/SCHEDULING.md
    "Automatic recovery after infrastructure loss"): once the loss stands for `wait_s` without a replacement attempt,
    the run continues from its newest acknowledged checkpoint pointer on the next class of its preference
    (jobs.recover), within `max_recoveries` and `deadline_s`. Returns the successors' ids. Control API only."""
    if not FLEET_MANAGER or not attempts.enabled():
        return []
    moved = []
    now = datetime.now(timezone.utc)
    try:
        namespaces = [x.metadata.name for x in kube.core().list_namespace(label_selector=f"{cfg.LABEL}/tenant").items]
    except Exception:  # noqa: BLE001
        return []
    for ns in namespaces:
        try:
            items = _run_jobs(ns)
        except Exception:  # noqa: BLE001
            continue
        for item in items:
            md = item["metadata"]
            ann = md.get("annotations") or {}
            name = md["name"]
            try:
                ex = json.loads(ann.get(f"{cfg.LABEL}/execution") or "null") or {}
            except ValueError:
                ex = {}
            rec = ex.get("recovery") or {}
            if (
                rec.get("on_infrastructure_loss") != "recover"
                or not jobs.is_manager_job(item)
                or status.finished(item)
                or ann.get(f"{cfg.LABEL}/successor")
                or ann.get(f"{cfg.LABEL}/cancelled")
            ):
                continue
            try:
                job = jobs.get(ns, name, REGION)
                root = ann.get(f"{cfg.LABEL}/resumed-from") or name
                op = jobs.normalise(job, records=attempts.for_operation(root))
                lost, why = _lost(op, job)
                if lost is None:
                    if ann.get(f"{cfg.LABEL}/lost-at"):  # back on its own class: the loss is over
                        _annotate(ns, name, {f"{cfg.LABEL}/lost-at": None})
                    continue
                lost_at = ann.get(f"{cfg.LABEL}/lost-at")
                if not lost_at:
                    _annotate(ns, name, {f"{cfg.LABEL}/lost-at": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
                    lost_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                if (now - datetime.fromisoformat(lost_at.replace("Z", "+00:00"))).total_seconds() < int(
                    rec.get("wait_s") or 0
                ):
                    continue
                tenant = md["labels"][f"{cfg.LABEL}/tenant"]
                marks = attempts._marks(name)
                if marks and marks[1]:
                    continue  # cancelled: never resurrected
                stop = None
                pointer = (attempts.latest_checkpoint(root) or {}).get("checkpoint_ref")
                if not pointer:
                    stop = "no acknowledged checkpoint pointer: the run cannot continue elsewhere (POST :resume after a cancel)"
                done = jobs.recoveries(ns, tenant, root)
                if not stop and done >= int(rec.get("max_recoveries") or 1):
                    stop = f"recovery exhausted: {done} of {rec.get('max_recoveries')} automatic continuations used"
                remaining_s = None
                if rec.get("deadline_s"):
                    root_job = job if root == name else jobs.get(ns, root, REGION)
                    born = datetime.fromisoformat(str(root_job["metadata"]["creationTimestamp"]).replace("Z", "+00:00"))
                    remaining_s = int(born.timestamp() + int(rec["deadline_s"]) - now.timestamp())
                    if not stop and remaining_s <= 0:
                        stop = f"recovery deadline passed: {rec['deadline_s']} s after the root operation was created"
                m = catalog.require(md["labels"].get(f"{cfg.LABEL}/model", ""))
                pref = (ex.get("placement") or {}).get("gpu_preference") or jobs.gpu_classes(m)
                current = (
                    lost.get("gpu_class")
                    or md.get("labels", {}).get(f"{cfg.LABEL}/gpu-class")
                    or (pref[0] if pref else None)
                )
                remaining = pref[pref.index(current) + 1 :] if current in pref else list(pref)
                next_class = remaining[0] if remaining else current
                if not stop and not next_class:
                    stop = "no GPU class to continue on"
                if stop:
                    if ann.get(f"{cfg.LABEL}/recovery") != stop:
                        _annotate(ns, name, {f"{cfg.LABEL}/recovery": stop})
                    continue
                placement = (
                    execution.narrow_placement(
                        {"profile": jobs.profile_of(m), "classes": [next_class], "regions": m["regions"]}, ex
                    )
                    or {}
                )
                placement["classes"] = [next_class]
                new, created = continue_run(
                    root,
                    lambda admit: jobs.recover(
                        ns,
                        name,
                        REGION,
                        m,
                        placement,
                        pointer,
                        int(lost.get("epoch") or 0),
                        next_class,
                        remaining or [next_class],
                        why,
                        timeout_s=remaining_s,
                        admit=admit,
                    ),
                    ns=ns,
                )
                marks = attempts._marks(name)
                if marks and marks[1]:  # cancelled while we were moving it: cancel the successor, too
                    attempts.mark_cancelled(new["metadata"]["name"])
                    settle_deleted(jobs.cancel(ns, new["metadata"]["name"], REGION))  # never started: deleted
                    _annotate(
                        ns,
                        name,
                        {
                            f"{cfg.LABEL}/cancelled": "true",
                            f"{cfg.LABEL}/recovery": "cancelled while recovering: the successor was withdrawn",
                        },
                    )
                    continue
                if created:
                    moved.append(new["metadata"]["name"])
                    log.info("recovery: %s/%s -> %s on %s (%s)", ns, name, new["metadata"]["name"], next_class, why)
            except Exception:  # noqa: BLE001
                log.exception("recovery of %s/%s failed", ns, name)
    return moved


def _annotate(ns: str, name: str, annotations: dict) -> None:
    kube.batch().patch_namespaced_job(name, ns, {"metadata": {"annotations": annotations}})


async def _reconcile_loop():
    while True:
        try:
            await asyncio.to_thread(idle_shutdown)
            await asyncio.to_thread(fallback_switch)
            await asyncio.to_thread(recovery_switch)
            await asyncio.to_thread(unschedulable_switch)
            await asyncio.to_thread(reconcile_models)
            await asyncio.to_thread(release_stale_reservations)
            await asyncio.to_thread(gate_stages)
            await asyncio.to_thread(sweep_sessions)
            await asyncio.to_thread(sweep_children)
            await asyncio.to_thread(sweep_executions)
            await asyncio.to_thread(keys.retire_mirrors)
            await asyncio.to_thread(reclaim_work_volumes)
        except Exception:
            log.exception("model reconciliation unavailable")
        await asyncio.sleep(15)


def gate_stages() -> list[str]:
    """Stage composition (docs/JOBS.md): admit waiting stages whose dependencies delivered, cancel what cannot run,
    close graphs that are over (stages.gate). Needs the ledger (the database)."""
    if not db.enabled():
        return []
    import stages

    return stages.gate()


def sweep_children() -> list[str]:
    """Endpoint children (docs/JOBS.md "Endpoint children"): ended children recorded, the cancel cascade of a cancelled
    or finished root repeated durably (children.sweep). Nothing without the database."""
    import children

    return children.sweep()


def sweep_sessions() -> list[str]:
    """Live sessions (docs/API.md "Sessions"): expired claims, unattached bindings, deadlines, lost workers, unsettled
    ends (sessions.sweep). Nothing without the feature or the database."""
    import sessions

    return sessions.sweep()


def sweep_executions() -> list[str]:
    """Native execution leases (docs/API.md): an attached worker whose pod (exact UID) is gone or terminated is lost;
    nothing else moves an attached execution."""
    if not db.enabled():
        return []
    import leases

    return leases.sweep()


def release_stale_reservations() -> list[str]:
    """A key reservation whose operation no longer exists (a Job deleted before billing accounted it, a submission
    that failed after admission) is released after RESERVATION_STALE_S; billing releases the rest with the attempt's
    GPU seconds. A run's reservation is its root's: it goes when every member's Job is gone, each member settled at
    zero when it never held a GPU (no bound) and as `unknown` otherwise (a GPU attempt that vanished unmeasured is
    not zero). Control API only. Returns `<operation>` for a released reservation, `<operation>:unknown` for one
    whose GPU time stays on record."""
    if not db.enabled():
        return []
    out = []
    for operation, tenant in keys.stale_reservations(RESERVATION_STALE_S):
        ns = f"{cfg.TENANT_NS_PREFIX}{tenant}"
        members = keys.run_members(operation) if hasattr(keys, "run_members") else []
        if not members:
            try:
                jobs.find(ns, operation)
            except HTTPException as e:
                if e.status_code == 404:
                    keys.release(operation, None)
                    out.append(operation)
            continue
        unknown = False
        for m in members:
            if m["state"] != "active":
                continue
            try:
                jobs.find(ns, m["operation"])
                break  # a member's Job still exists: billing settles it
            except HTTPException as e:
                if e.status_code != 404:
                    break
            keys.settle_run_member(m["operation"], 0.0 if not m["gpu_bound"] else None)
            unknown = unknown or bool(m["gpu_bound"])
        else:
            out.append(f"{operation}:unknown" if unknown else operation)
    return out


# --- work volumes: released on proof, never with the history (docs/JOBS.md "Work volume lifecycle") ---------------

WORK_VOLUME_ANN = f"{cfg.LABEL}/work-volume"  # on the Job: released|<reason>|<ts> or retained|<reason>|<ts>
WORK_RE = re.compile(r"^op-[0-9a-f]{16}(?:-s-[a-z0-9-]+|-c-[0-9a-f]+|-x\d+|-r\d+)?-work$")
_reclaim = {"at": 0.0}


def work_volume_mark(job: dict | None) -> dict | None:
    """The recorded lifecycle of an operation's work volume: {state, reason, at} from WORK_VOLUME_ANN, None when
    nothing was recorded (the volume is as the uploader left it)."""
    v = ((job or {}).get("metadata", {}).get("annotations") or {}).get(WORK_VOLUME_ANN) or ""
    state, _, rest = v.partition("|")
    if state not in ("released", "retained"):
        return None
    reason, _, at = rest.partition("|")
    return {"state": state, "reason": reason, "at": at or None}


def _work_decision_for(
    job: dict | None, rows: list[dict], tenant: str | None, jobs_by_name: dict | None = None
) -> tuple[str, str]:
    """('keep' | 'release', reason) for one terminal operation of a claim, from the API's own status projection and the
    operation's durable attempt rows; a gone Job is judged by its rows alone (synthesize), no rows = unknown. Kept
    only while an export is unresolved or the state is unknown: the volume is scratch (docs/JOBS.md)."""
    if job is None:
        if not rows or not tenant:
            return "keep", "unknown: Job gone, no attempt record"
        import admissions

        st = (attempts.synthesize(rows[0]["operation"], tenant) or {}).get("status")
        rec = admissions.get(rows[0]["operation"])
        mode = ((rec or {}).get("labels") or {}).get(f"{cfg.LABEL}/mode") or "run"
        if st is None:
            return "keep", "unknown: Job gone, no attempt record"
    else:
        st = jobs.normalise(job, None, rows)["status"]
        mode = (job["metadata"].get("labels") or {}).get(f"{cfg.LABEL}/mode") or "run"
    if st == "SUCCEEDED":
        return "release", "delivered"
    if st == "EXPORT_FAILED":
        return "keep", "export pending"
    own = [r for r in rows if not job or r.get("operation") == job["metadata"]["name"]] or rows
    if own and own[-1].get("export_state") == "failed":
        # an undelivered capture or output on the volume (the uploader's last record says the export failed): the
        # transfer-only retry (`:export`, app.py export_again / export_async) delivers it from this volume, whatever
        # the handle's status (a FAILED endpoint call's exit 2/5 capture, a FAILED run's partial output)
        return "keep", "export pending"
    pending = _finalizer_pending(job, own, rows, jobs_by_name)
    if pending:
        return pending
    if st == "SUPERSEDED":
        return "release", "superseded"
    if st not in ("FAILED", "CANCELLED"):
        return "keep", "active"
    if mode == "async":
        return (
            "release",
            "call ended",
        )  # an endpoint call never resumes; its capture went with the delivery or the export retry
    if not any(r.get("started_at") for r in rows):
        return "release", "never started"
    # a run that ran and ended: its records are durable (acknowledged by the API), its outputs are in the customer's
    # storage (managed I/O: the uploader's verified sync, else "export pending" above; application I/O: the
    # application's own delivery and checkpoints). The disk is scratch, never the only copy of anything a resume
    # needs: a resume renders the run again on a fresh volume from the application's checkpoint pointer or from the
    # inputs (jobs.resume). Keeping it would retain customer data on platform disks indefinitely (decision 2026-10-21).
    return "release", "ended"


def _finalizer_pending(
    job: dict | None, own: list[dict], lineage: list[dict], jobs_by_name: dict | None = None
) -> tuple[str, str] | None:
    """The existing finalizer contract (docs/JOBS.md "Attempts and finalization", catalog `job.finalize`): a run whose
    main exited with a declared `on_exit_codes` value finished its compute and left the delivery to the CPU finalizer
    (`<root>-x<n>`, attempt role `finalize`) on the SAME scratch. Such a volume is kept until THIS operation's
    finalizer delivered: a finalizer counts only when it is later than the current compute attempt (row revision;
    the lineage's rows are ordered) and, when its Job is readable, its `export-of` names this operation (an older
    finalizer delivered a predecessor, never the current run's output). A failed finalizer keeps the volume for the
    next `:export`. Judged from the catalog's declaration and the attempt rows, never an exit-code or model constant."""
    if not job or not own:
        return None
    last = own[-1]
    if last.get("role") == "finalize" or last.get("exit_code") in (None, 0):
        return None
    name = job["metadata"]["name"]
    model = (job["metadata"].get("labels") or {}).get(f"{cfg.LABEL}/model")
    entry = catalog.get(model) if model else None
    codes = (((entry or {}).get("job") or {}).get("finalize") or {}).get("onExitCodes") or []
    if last.get("exit_code") not in codes:
        return None
    since = last.get("revision") or 0
    for f in lineage:
        if f.get("role") != "finalize" or (f.get("revision") or 0) <= since:
            continue  # not a finalizer, or one that ran before this compute attempt: it delivered a predecessor
        fj = (jobs_by_name or {}).get(f.get("operation"))
        if fj is not None and (fj["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/export-of") != name:
            continue  # a readable finalizer Job that names another operation
        if f.get("status") == "succeeded" and (
            f.get("export_state") == "succeeded" or (f.get("export_state") == "n/a" and f.get("result_ref"))
        ):
            return "release", "delivered by finalizer"
    return "keep", "finalizer pending"


def work_volume_decision(
    ns: str, claim: dict, mounted: set, find=None, records_of=None, related: list[str] | None = None
) -> tuple[str, str, list]:
    """What to do with one per-operation work claim: ('keep' | 'release', reason, [(job, cluster) of its Jobs]).
    A mounted or deleting claim and a live referencing Job (the operation, a continuation, a finalizer, anything the
    dispatcher recorded on it, anything the tenant listing shows naming the claim: `related`, a queued same-volume
    resume the dispatcher has not recorded yet) keep it; otherwise every terminal referencing operation is judged
    (_work_decision_for) and the claim goes only when all of them agree. Any read that fails means unknown: kept."""
    find = find or jobs.find
    records_of = records_of or attempts.for_operations
    md = claim["metadata"]
    name = md["name"]
    if name in mounted:
        return "keep", "mounted", []
    if md.get("deletionTimestamp"):
        return "keep", "releasing", []
    refs = {(md.get("labels") or {}).get(f"{cfg.LABEL}/operation") or name[: -len("-work")]}
    refs |= {j for j in ((md.get("annotations") or {}).get(f"{cfg.LABEL}/jobs") or "").split(",") if j}
    refs |= {o["name"] for o in md.get("ownerReferences") or [] if o.get("kind") == "Job" and o.get("name")}
    refs |= set(related or [])
    found: list = []
    for r in sorted(refs):
        try:
            j, _ = find(ns, r)
        except HTTPException as e:
            if e.status_code != 404:
                return "keep", f"unknown: {r} unreadable ({e.status_code})", []
            found.append((r, None))
            continue
        except Exception as e:  # noqa: BLE001 - a cluster that cannot be read is unknown, never absent
            return "keep", f"unknown: {r} unreadable ({type(e).__name__})", []
        ann = j["metadata"].get("annotations") or {}
        if not jobs.job_terminal(j) and not ann.get(f"{cfg.LABEL}/successor"):
            return "keep", "active", []
        found.append((r, j))
    try:
        rows = records_of({r for r, _ in found})
    except Exception as e:  # noqa: BLE001 - the ledger unavailable: unknown
        return "keep", f"unknown: attempt records unavailable ({type(e).__name__})", []
    tenant = ns.removeprefix(cfg.TENANT_NS_PREFIX)
    reasons = []
    for r, j in found:
        action, why = _work_decision_for(j, rows.get(r, []), tenant, {n: jj for n, jj in found if jj})
        if action == "keep":
            return "keep", why, [(r, j) for r, j in found]
        reasons.append(why)
    return "release", "; ".join(sorted(set(reasons))) or "no reference", [(r, j) for r, j in found]


def claim_users(ns: str, tenant: str) -> dict[str, list[str]] | None:
    """Every live Job of the tenant namespace in EVERY cluster that names a work claim (its `pvc` annotation or its pod
    template's claim), fresh from the API servers, never a cached listing (a cached generation can be stale or miss a
    region: a queued same-volume successor absent from it would let its predecessor's release go through). None when
    any cluster could not be read: partial knowledge releases nothing (fail closed)."""
    out: dict[str, list[str]] = {}
    for r in kube.regions():
        try:
            items = jobs._json_items(
                kube.batch(r).list_namespaced_job, ns, label_selector=f"{cfg.LABEL}/tenant={tenant}"
            )
        except Exception as e:  # noqa: BLE001 - unreadable: unknown, never absent
            log.warning("work volumes %s/%s: Jobs of %s unreadable (%s)", r, ns, tenant, type(e).__name__)
            return None
        for j in items:
            if jobs.is_mirror(j):
                continue  # the worker's copy of a manager Job: the manager is the reference
            jn = j["metadata"]["name"]
            names = {(j["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/pvc")} | {
                (v.get("persistentVolumeClaim") or {}).get("claimName")
                for v in (((j.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or []
            }
            for c in names:
                if c:
                    out.setdefault(c, []).append(jn)
    return out


def _stamp_jobs(ns: str, found: list, value: str) -> None:
    """Record the volume's lifecycle on every referencing Job that exists (its own cluster), once per value."""
    for r, j in found:
        if j is None or (j["metadata"].get("annotations") or {}).get(WORK_VOLUME_ANN) == value:
            continue
        for region in kube.regions():
            try:
                kube.batch(region).patch_namespaced_job(r, ns, {"metadata": {"annotations": {WORK_VOLUME_ANN: value}}})
                break
            except Exception:  # noqa: BLE001 - not on this cluster or unreadable: the next pass records it
                continue


def reclaim_work_volumes(now: float | None = None) -> list[str]:
    """Every WORK_RECLAIM_EVERY_S on the control API: release the per-operation work claims (`<op>-work`, every
    tenant namespace, every cluster) whose operations are over and proven not to need them (work_volume_decision),
    with uid and resourceVersion preconditions under the operation lock; record the decision on the Jobs. Returns
    `<cluster>/<ns>/<claim>` for each released claim. Shared claims, assets and caches never match the name rule."""
    if not FLEET_MANAGER:
        return []
    now = time.time() if now is None else now
    if now - _reclaim["at"] < cfg.WORK_RECLAIM_EVERY_S:
        return []
    _reclaim["at"] = now
    out: list[str] = []
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for region in kube.regions():
        core = kube.core(region)
        for ns in jobs.tenant_namespaces():
            try:
                claims = jobs._json_items(
                    core.list_namespaced_persistent_volume_claim, ns, label_selector=f"{cfg.LABEL}/operation"
                )
                claims = [c for c in claims if WORK_RE.match(c["metadata"]["name"])]
                if not claims:
                    continue
                pods = jobs._json_items(core.list_namespaced_pod, ns)
            except Exception as e:  # noqa: BLE001 - unreadable namespace: nothing is judged there this pass
                log.warning("work volumes %s/%s: list failed (%s)", region, ns, type(e).__name__)
                continue
            # a mount is a pod that can still run (a terminal pod's claim reference only delays the deletion through
            # pvc-protection; billing.cleanup_released_pods and the dispatcher remove such pods once the claim is deleting)
            mounted = {
                (v.get("persistentVolumeClaim") or {}).get("claimName")
                for p in pods
                if (p.get("status") or {}).get("phase") not in ("Succeeded", "Failed")
                for v in p["spec"].get("volumes") or []
            }
            tenant = ns.removeprefix(cfg.TENANT_NS_PREFIX)
            related = claim_users(ns, tenant)  # fresh and complete, or None: then nothing in this namespace is released
            if related is None:
                continue
            released_here = 0
            for claim in claims:
                name = claim["metadata"]["name"]
                if released_here >= cfg.WORK_RECLAIM_MAX_PER_PASS:
                    break  # the rest next pass: every release re-lists the tenant's Jobs under the lock
                try:
                    action, why, found = work_volume_decision(ns, claim, mounted, related=related.get(name))
                    if action == "keep":
                        if why not in ("mounted", "active", "releasing"):
                            _stamp_jobs(ns, found, f"retained|{why}|{stamp}")
                        continue
                    # the release is decided AGAIN under the claim's operation lock, the boundary every continuation
                    # (app.continue_run) holds while it checks, records itself on and creates against the claim: the
                    # claim is re-read (gone or deleting: nothing to do; a new reference recorded on it: kept), then
                    # deleted with the fresh uid and resourceVersion as preconditions
                    root = (claim["metadata"].get("labels") or {}).get(f"{cfg.LABEL}/operation") or name
                    with keys.operation_lock(root) if db.enabled() else nullcontext():
                        try:
                            fresh = core.api_client.sanitize_for_serialization(
                                retry(core.read_namespaced_persistent_volume_claim, name, ns)
                            )
                        except ApiException as e:
                            if e.status == 404:
                                continue
                            raise
                        if fresh["metadata"].get("deletionTimestamp"):
                            continue
                        now_users = claim_users(
                            ns, tenant
                        )  # fresh again, under the lock: the proof the delete rests on
                        if now_users is None:
                            continue
                        action, why, found = work_volume_decision(ns, fresh, mounted, related=now_users.get(name))
                        if action == "keep":
                            continue  # a continuation took it under the lock: judged again next pass
                        from kubernetes.client import V1DeleteOptions, V1Preconditions

                        retry(
                            core.delete_namespaced_persistent_volume_claim,
                            name,
                            ns,
                            body=V1DeleteOptions(
                                preconditions=V1Preconditions(
                                    uid=fresh["metadata"]["uid"], resource_version=fresh["metadata"]["resourceVersion"]
                                )
                            ),
                        )
                    _stamp_jobs(ns, found, f"released|{why}|{stamp}")
                    log.info("work volume %s/%s/%s released (%s)", region, ns, name, why)
                    out.append(f"{region}/{ns}/{name}")
                    released_here += 1
                except ApiException as e:
                    if e.status not in (
                        404,
                        409,
                    ):  # gone or changed under us (a resume took it): the next pass judges again
                        log.warning("work volume %s/%s/%s: %s %s", region, ns, name, e.status, e.reason)
                except Exception as e:  # noqa: BLE001 - one claim's failure never stops the pass; kept, judged again
                    log.warning("work volume %s/%s/%s deferred (%s)", region, ns, name, type(e).__name__)
    return out
