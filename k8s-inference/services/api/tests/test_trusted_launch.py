"""Trusted launch (docs/JOBS.md "Trusted launch"): a model administrator's typed declaration `trusted_launch` for a
launcher image that isolates authored code itself; the platform validates it before persistence and projects it onto
the model container only (a run's main on every rank and region, an endpoint's serving container on every variant,
region and both object kinds). Without it every render is byte-identical to before; nothing a request can set."""

import copy

import pytest
from fastapi import HTTPException

import catalog
import jobs
import models
import render
import render_endpoint
from test_api import ADMIN, H, _main, admin_keys, client, cluster  # noqa: F401
from test_finalizer import failed_run  # noqa: F401

PSA_BASELINE_ADDS = {
    "AUDIT_WRITE", "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "MKNOD", "NET_BIND_SERVICE", "SETFCAP",
    "SETPCAP", "SETGID", "SETUID", "SYS_CHROOT",
}  # fmt: skip
TL = {"capabilities": ["CHOWN", "KILL", "SETGID", "SETUID"], "user": 0, "group": 0}
EXPECTED = {
    "runAsUser": 0,
    "runAsGroup": 0,
    "runAsNonRoot": False,
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "KILL", "SETGID", "SETUID"]},
    "seccompProfile": {"type": "RuntimeDefault"},
}
JOB = {
    "id": "launcher-batch",
    "kind": "job",
    "image": "ghcr.io/org/launcher:1",
    "command": "python -m launcher",
    "gpu": {"count": 1, "classes": ["h100"]},
}
ENDPOINT = {
    "id": "launcher-live",
    "kind": "endpoint",
    "image": "ghcr.io/org/live:1",
    "protocol": "http",
    "port": 8080,
    "gpu": {"count": 1, "classes": ["h100"]},
}


def test_the_platform_set_is_closed_and_inside_pod_security_baseline():
    from typing import get_args

    from schemas import TrustedLaunchSpec

    assert set(render.TRUSTED_LAUNCH_CAPABILITIES) <= PSA_BASELINE_ADDS
    literal = TrustedLaunchSpec.model_fields["capabilities"].annotation
    assert set(get_args(get_args(literal)[0])) == set(render.TRUSTED_LAUNCH_CAPABILITIES)
    assert render.trusted_launch_context(TL) == EXPECTED
    assert render.trusted_launch_context({"capabilities": ["SETUID", "CHOWN"], "user": 1000, "group": 1000}) == {
        **EXPECTED,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "runAsNonRoot": True,
        "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "SETUID"]},
    }
    assert render.CONTAINER_HARDENING == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }  # the default stays what it was


def test_invalid_and_contradictory_declarations_fail_before_write():
    bad = [
        ({"trusted_launch": "yes"}, "trusted_launch"),
        ({"trusted_launch": {}}, "capabilities"),
        ({"trusted_launch": {"capabilities": []}}, "capabilities"),
        ({"trusted_launch": {"capabilities": ["SYS_ADMIN"]}}, "capabilities"),
        ({"trusted_launch": {"capabilities": ["DAC_OVERRIDE"]}}, "capabilities"),
        ({"trusted_launch": {"capabilities": ["SETPCAP"]}}, "capabilities"),
        ({"trusted_launch": {"capabilities": ["SYS_PTRACE"]}}, "capabilities"),
        ({"trusted_launch": {"capabilities": ["chown"]}}, "capabilities"),
        ({"trusted_launch": {"capabilities": ["CHOWN", "CHOWN"]}}, "duplicate"),
        ({"trusted_launch": {"capabilities": ["CHOWN"], "user": -1}}, "user"),
        ({"trusted_launch": {"capabilities": ["CHOWN"], "user": "root"}}, "user"),
        # strict integers: nothing is coerced into an id on a security declaration (Root's review)
        ({"trusted_launch": {"capabilities": ["CHOWN"], "user": True}}, "user"),
        ({"trusted_launch": {"capabilities": ["CHOWN"], "user": False}}, "user"),
        ({"trusted_launch": {"capabilities": ["CHOWN"], "user": "1000"}}, "user"),
        ({"trusted_launch": {"capabilities": ["CHOWN"], "user": 1000.0}}, "user"),
        ({"trusted_launch": {"capabilities": ["CHOWN"], "group": True}}, "group"),
        ({"trusted_launch": {"capabilities": ["CHOWN"], "group": "0"}}, "group"),
        ({"trusted_launch": {"capabilities": ["CHOWN"], "group": 1.5}}, "group"),
        # representable ids only: Kubernetes runAsUser/runAsGroup are int64 (Root's review); max+1 is refused pre-write
        ({"trusted_launch": {"capabilities": ["CHOWN"], "user": 2**63}}, "user"),
        ({"trusted_launch": {"capabilities": ["CHOWN"], "group": 2**63}}, "group"),
        ({"trusted_launch": {"capabilities": ["CHOWN"], "user": 2**64}}, "user"),
        ({"trusted_launch": {"capabilities": [True]}}, "capabilities"),
        ({"trusted_launch": {"capabilities": "CHOWN"}}, "capabilities"),
        ({"trusted_launch": {"capabilities": ["CHOWN"], "privileged": True}}, "privileged"),
        ({"trusted_launch": {"capabilities": ["CHOWN"], "drop": []}}, "drop"),
        ({"trusted_launch": TL, "runAsNonRoot": True}, "contradicts"),
        ({"trusted_launch": TL, "job": {"runAsNonRoot": True}}, "contradicts"),
    ]
    for extra, needle in bad:
        with pytest.raises(HTTPException) as e:
            models.validate({**JOB, **extra})
        assert e.value.status_code == 400 and needle in e.value.detail, (extra, e.value.detail)
    for extra in ({"containerSecurityContext": {"readOnlyRootFilesystem": True}}, {"securityContext": {"fsGroup": 1}}):
        with pytest.raises(HTTPException) as e:
            models.validate({**ENDPOINT, "trusted_launch": TL, **extra})
        assert e.value.status_code == 400 and "one author" in e.value.detail
    # the representable boundary is accepted exactly, for both ids
    big = models.validate({**JOB, "trusted_launch": {"capabilities": ["KILL"], "user": 2**63 - 1, "group": 2**63 - 1}})
    assert big["trusted_launch"] == {"capabilities": ["KILL"], "user": 2**63 - 1, "group": 2**63 - 1}
    assert render.trusted_launch_context(big["trusted_launch"])["runAsUser"] == 2**63 - 1
    # a non-root launcher uid is consistent with runAsNonRoot
    ok = models.validate(
        {**JOB, "trusted_launch": {"capabilities": ["SETUID", "SETGID"], "user": 1000}, "runAsNonRoot": True}
    )
    assert ok["trusted_launch"] == {"capabilities": ["SETGID", "SETUID"], "user": 1000, "group": 0}
    # the validated form is canonical (platform order, defaults filled) and survives the entry projection
    e = models.to_entry({**JOB, "trusted_launch": {"capabilities": ["SETUID", "CHOWN"]}})
    assert e["trustedLaunch"] == {"capabilities": ["CHOWN", "SETUID"], "user": 0, "group": 0}
    assert e["spec"]["trusted_launch"] == e["trustedLaunch"]
    assert "trustedLaunch" not in models.to_entry(JOB)


def test_declared_runs_render_main_only_and_omitted_runs_are_byte_identical(client, admin_keys, monkeypatch):
    c, fake = client
    created = c.post("/v1/models", json=JOB, headers=ADMIN)
    assert created.status_code == 201, created.text
    plain = c.post("/v1/models/launcher-batch:invoke", json={"mode": "run", "input": {}}, headers=H)
    assert plain.status_code == 202, plain.text
    pod0 = fake.jobs[("tenant-demo", plain.json()["id"])]["spec"]["template"]["spec"]
    assert _main(fake.jobs[("tenant-demo", plain.json()["id"])])["securityContext"] == render.CONTAINER_HARDENING
    # declare it (PUT with If-Match on the stored version): the full spec and the history carry it
    r = c.put(
        "/v1/models/launcher-batch",
        json={**JOB, "trusted_launch": TL},
        headers={**ADMIN, "If-Match": str(created.json()["version"])},
    )
    assert r.status_code == 200, r.text
    assert c.get("/v1/models/launcher-batch", headers=ADMIN).json()["spec"]["trusted_launch"] == TL
    hist = c.get("/v1/models/launcher-batch/history", headers=ADMIN).json()
    by_version = {h["version"]: h for h in hist}
    assert by_version[2]["spec"]["trusted_launch"] == TL and by_version[2]["action"] == "update"
    assert "trusted_launch" not in by_version[1]["spec"] and by_version[1]["action"] == "create"
    # a stale If-Match never writes a declaration
    stale = c.put(
        "/v1/models/launcher-batch",
        json={**JOB, "trusted_launch": {"capabilities": ["KILL"]}},
        headers={**ADMIN, "If-Match": "1"},
    )
    assert stale.status_code in (409, 412), stale.text
    assert c.get("/v1/models/launcher-batch", headers=ADMIN).json()["spec"]["trusted_launch"] == TL
    # an invalid declaration is refused before any write: the stored definition is unchanged
    bad = c.put(
        "/v1/models/launcher-batch", json={**JOB, "trusted_launch": {"capabilities": ["SYS_ADMIN"]}}, headers=ADMIN
    )
    assert (
        bad.status_code == 400
        and c.get("/v1/models/launcher-batch", headers=ADMIN).json()["spec"]["trusted_launch"] == TL
    )
    # the run: main gets exactly the projection, every other container exactly what it had
    r = c.post("/v1/models/launcher-batch:invoke", json={"mode": "run", "input": {}}, headers=H)
    assert r.status_code == 202, r.text
    job = fake.jobs[("tenant-demo", r.json()["id"])]
    pod = job["spec"]["template"]["spec"]
    assert _main(job)["securityContext"] == EXPECTED
    others = {
        ctn["name"]: ctn["securityContext"]
        for ctn in pod["initContainers"] + pod["containers"]
        if ctn["name"] != "main"
    }
    before = {
        ctn["name"]: ctn["securityContext"]
        for ctn in pod0["initContainers"] + pod0["containers"]
        if ctn["name"] != "main"
    }
    assert others == before and others and all(sc["capabilities"] == {"drop": ["ALL"]} for sc in others.values())
    assert pod["securityContext"] == pod0["securityContext"]  # pod-level: fsGroup 10001, RuntimeDefault, unchanged
    # the whole pod minus main is byte-identical to the undeclared render (names differ only by the operation id)
    import json as _json

    def strip(p, op):  # the pod minus its containers, with the operation id (claim name) neutralised
        return _json.loads(
            _json.dumps({k: v for k, v in p.items() if k not in ("containers", "initContainers")}).replace(op, "OP")
        )

    assert strip(pod, r.json()["id"]) == strip(pod0, plain.json()["id"])
    # a request cannot set or override it
    forged = c.post(
        "/v1/models/launcher-batch:invoke",
        json={"mode": "run", "input": {}, "execution": {"trusted_launch": {"capabilities": ["SYS_ADMIN"]}}},
        headers=H,
    )
    assert forged.status_code in (400, 422)
    # the contradiction is refused at render too (a catalog-managed job class that says runAsNonRoot)
    m = copy.deepcopy(catalog.get("launcher-batch"))
    m["job"]["runAsNonRoot"] = True
    with pytest.raises(HTTPException) as e:
        jobs._main_security_context(m, m["job"])
    assert e.value.status_code == 400


def test_a_gang_renders_the_declaration_on_every_ranks_main_and_fallback_regions_alike(client, admin_keys, monkeypatch):
    """`job.nodes: 2` (the documented gang projection on the entry's job block; the API's top-level `nodes` is an
    endpoint projection) renders a JobSet: one indexed Job of exactly `nodes` whole-node pods; every rank's main
    carries the projection; everything but main (init and provision containers with their own existing contexts,
    sidecars, pod-level context, volumes) is byte-identical to the undeclared render of the same class; the same in
    every region the run may land in or fall back to, and under fleet placement."""
    import kube
    from test_api import MULTINODE_FLEET

    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: MULTINODE_FLEET)
    spec = {**JOB, "id": "launcher-gang", "gpu": {"count": 8, "classes": ["h100"]}, "trusted_launch": TL}
    assert c.post("/v1/models", json=spec, headers=ADMIN).status_code == 201, "gang definition"
    m = copy.deepcopy(catalog.get("launcher-gang"))
    m["job"].update({"nodes": 2, "interconnect": "none"})  # as a catalog-managed gang class declares it
    assert int(m["job"]["nodes"]) == 2 and m["trustedLaunch"] == TL
    u = copy.deepcopy(m)
    u.pop("trustedLaunch")  # the same class without the declaration: the byte reference for everything but main
    monkeypatch.setattr(catalog, "get", lambda name, _m=m, _g=catalog.get: _m if name == "launcher-gang" else _g(name))

    def pod_of(doc):
        assert doc["kind"] == "JobSet"
        rjs = doc["spec"]["replicatedJobs"]
        assert len(rjs) == 1 and rjs[0]["replicas"] == 1
        tmpl = rjs[0]["template"]["spec"]
        assert tmpl["parallelism"] == 2 and tmpl["completions"] == 2  # exactly `nodes` ranks
        return tmpl["template"]["spec"]

    def split(pod):
        mains = [ctn for ctn in pod["containers"] if ctn["name"] == "main"]
        assert len(mains) == 1
        rest = {
            **{k: v for k, v in pod.items() if k not in ("containers", "initContainers")},
            "containers": [ctn for ctn in pod["containers"] if ctn["name"] != "main"],
            "initContainers": pod.get("initContainers", []),
            "main_without_security": {k: v for k, v in mains[0].items() if k != "securityContext"},
        }
        return mains[0]["securityContext"], rest

    def render_in(model, region):
        return jobs.build_run(
            "op-gang-render",
            model,
            {},
            "demo",
            None,
            None,
            None,
            None,
            region,
            {"output_prefix": "s3://b/operations/op-gang-render"},
        )

    r = c.post("/v1/models/launcher-gang:invoke", json={"mode": "run", "input": {}}, headers=H)
    assert r.status_code == 202, r.text
    sc, _rest = split(pod_of(fake.custom[("tenant-demo", "jobsets", r.json()["id"])]))
    assert sc == EXPECTED
    regions = list(dict.fromkeys(list(m["regions"]) + [None]))
    assert len([x for x in regions if x]) >= 1
    for region in regions:
        declared_sc, declared_rest = split(pod_of(render_in(m, region)))
        undeclared_sc, undeclared_rest = split(pod_of(render_in(u, region)))
        assert declared_sc == EXPECTED and undeclared_sc == render.CONTAINER_HARDENING, region
        assert declared_rest == undeclared_rest, region  # byte parity of everything but main's context


def test_endpoints_render_the_serving_container_on_every_variant_and_region_and_omitted_is_unchanged(monkeypatch):
    import kube
    from test_variants import SPEC, VARIANT_FLEET

    monkeypatch.setattr(kube, "fleet", lambda: VARIANT_FLEET)
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1" if c == "hub" else c)
    plain = catalog.normalise(models.to_entry(SPEC))
    declared = catalog.normalise(models.to_entry({**SPEC, "trusted_launch": TL}))

    def objects(entry):
        return [
            d
            for v in models.variants(entry, "hub")
            for d in render_endpoint.render(entry, "hub", "203.0.113.1.sslip.io", "ib", v, False)
        ]

    def serving(doc):
        if doc["kind"] == "InferenceService":
            return doc["spec"]["predictor"]["containers"]
        if doc["kind"] == "LeaderWorkerSet":
            lw = doc["spec"]["leaderWorkerTemplate"]
            return lw["leaderTemplate"]["spec"]["containers"] + lw["workerTemplate"]["spec"]["containers"]
        return []

    p, d = objects(plain), objects(declared)
    assert [(x["kind"], x["metadata"]["name"]) for x in p] == [(x["kind"], x["metadata"]["name"]) for x in d]
    seen = 0
    for a, b in zip(p, d):
        if a["kind"] in ("InferenceService", "LeaderWorkerSet"):
            for ca, cb in zip(serving(a), serving(b)):
                assert "securityContext" not in ca  # omitted: no explicit context, exactly as before
                assert cb["securityContext"] == EXPECTED
                seen += 1
            # the declaration changes nothing else on the object
            sa, sb = copy.deepcopy(a), copy.deepcopy(b)
            for cb in serving(sb):
                cb.pop("securityContext")
            assert sa == sb
        else:
            assert a == b  # policies, routes, services: byte-identical
    assert seen == 3  # chat, chat-spot, chat-cheap
    # a multi-node endpoint (LeaderWorkerSet): leader and worker containers alike
    multi = catalog.normalise(
        models.to_entry(
            {
                **ENDPOINT,
                "id": "launcher-lws",
                "nodes": 2,
                "interconnect": "none",
                "regions": ["hub"],
                "trusted_launch": TL,
            }
        )
    )
    lws = [
        d
        for v in models.variants(multi, "hub")
        for d in render_endpoint.render(multi, "hub", "203.0.113.1.sslip.io", "ib", v, False)
        if d["kind"] == "LeaderWorkerSet"
    ]
    assert lws and all(cn["securityContext"] == EXPECTED for cn in serving(lws[0]))
    # an entry that carries both authors is refused at render as well as at validation
    both = copy.deepcopy(declared)
    both["containerSecurityContext"] = {"readOnlyRootFilesystem": True}
    with pytest.raises(HTTPException):
        objects(both)


def _export_main(fake, xid):
    spec = fake.jobs[(NS_FIN, xid)]["spec"]["template"]["spec"]
    return spec, {ctn["name"]: ctn for ctn in spec["initContainers"] + spec["containers"]}


NS_FIN = "tenant-demo"


def test_an_export_never_inherits_the_declaration_finalizer_mode(failed_run, monkeypatch):
    """jobs.export_again rebuilds the original Job (main carries the declaration) and then makes main the CPU
    finalizer: its context must be exactly the undeclared main's (drop ALL, no adds, no uid 0 by declaration), as
    before the declaration existed; uploader and bridge-reset unchanged."""
    from test_finalizer import SPEC as FIN_SPEC
    from test_finalizer import _model as fin_model

    c, fake, op, rows, marks = failed_run
    declared = fin_model({**FIN_SPEC, "trusted_launch": TL})
    assert declared["trustedLaunch"] == TL
    monkeypatch.setattr(catalog, "get", lambda name: declared if name == "md" else None)
    r = c.post(f"/v1/operations/{op}:export", headers=H)
    assert r.status_code == 202, r.text
    spec, by = _export_main(fake, r.json()["id"])
    assert by["main"]["command"][-1] == FIN_SPEC["finalize"]["command"]  # the finalizer, CPU only
    assert by["main"]["securityContext"] == render.CONTAINER_HARDENING
    assert (
        "add" not in by["main"]["securityContext"]["capabilities"] and "runAsUser" not in by["main"]["securityContext"]
    )
    assert by["uploader"]["securityContext"] == render.RUNNER_SECURITY_CONTEXT
    assert all(ctn["securityContext"]["capabilities"] == {"drop": ["ALL"]} for ctn in spec["initContainers"])


def test_an_export_never_inherits_the_declaration_transfer_mode(failed_run, monkeypatch):
    """The transfer-only export (jobs.export_again without a finalizer): main is the runner image that exits 0; its
    context must be exactly the undeclared main's, never the declared launcher's; the uploader unchanged."""
    from test_finalizer import SPEC as FIN_SPEC
    from test_finalizer import _model as fin_model

    c, fake, op, rows, marks = failed_run
    base = {k: v for k, v in FIN_SPEC.items() if k != "finalize"}
    for model, label in ((fin_model(base), "undeclared"), (fin_model({**base, "trusted_launch": TL}), "declared")):
        doc, _ = jobs.export_again(
            "tenant-demo", op, "eu-north1", model, {"output_prefix": "s3://b/operations/x"}, None
        )
        spec = doc["spec"]["template"]["spec"]
        by = {ctn["name"]: ctn for ctn in spec["initContainers"] + spec["containers"]}
        assert by["main"]["image"] == jobs.RUNNER_IMAGE and by["main"]["command"] == ["/bin/sh", "-c", "true"], label
        assert by["main"]["securityContext"] == render.CONTAINER_HARDENING, label
        assert by["uploader"]["securityContext"] == render.RUNNER_SECURITY_CONTEXT, label
        assert all(ctn["securityContext"]["capabilities"] == {"drop": ["ALL"]} for ctn in spec["initContainers"]), label
