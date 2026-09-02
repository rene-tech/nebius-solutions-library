# Vendored NVIDIA ModelExpress chart

This directory vendors the Apache-2.0 Helm chart and Kubernetes CRDs from
[`ai-dynamo/modelexpress` v0.5.1](https://github.com/ai-dynamo/modelexpress/tree/v0.5.1/helm),
commit `eb5011575dcf56327578634f93a2ec2f7b5416fd`.

FS2 carries four narrow integration patches:

- correct the chart's stale default image tag from `0.3.0` to `0.5.1`;
- allow an immutable OCI digest instead of a mutable tag;
- expose the standard Deployment strategy so the managed RWO cache can use
  `Recreate` instead of deadlocking on a rolling multi-attach.
- correct the server log-level variable from the obsolete
  `MODEL_EXPRESS_LOGGING_LEVEL` spelling to upstream v0.5.1's
  `MODEL_EXPRESS_LOG_LEVEL`.

The chart remains disabled unless `deployment.acceleration.model_express.enabled`
is explicitly set. Update the upstream tag, commit, release notes, CRDs and these
patches together.
