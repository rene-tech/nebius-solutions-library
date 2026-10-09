# Independently release the GPU allocation observer

The optional `deployment.applications.control_plane.gpu_observer_image` setting
accepts a qualified `repository@sha256:digest`. Terraform passes it to the
workloads stage and Helm's `runtimeAttribution.image`. Its default is empty,
which preserves the existing behavior of following the control-plane image.

Pinning the observer keeps its entire Pod template unchanged when only the
gateway image changes. This avoids rotating observers on every GPU node for an
unrelated model-adapter or API release. During the scientific qualification
campaign, an already-terminating observer on a stopped preemptible node blocked
a gateway rollout until the unchanged Helm deadline expired. The node returned
naturally and the rollback completed; no forced deletion was needed.

This setting does not disable accounting or change DaemonSet availability,
resources, permissions, tolerations, or Helm deadlines. It does not solve stopped
nodes holding an observer's own rollout open. When observer code or its Pod
annotation protocol changes, qualify the new observer image against the deployed
lifecycle reader and update this pin explicitly. Preserve old image receipts.

Acceptance: render two different gateway digests with the same observer pin and
compare the complete DaemonSet; it must be identical. After deployment, verify
the observer digest, ready count, and fresh Pod-to-GPU observations independently
of the gateway's new image. A successful template test alone is not live evidence.
