# Install the workshop into an existing platform

This optional Terraform root is separate from the platform foundation/workload
states. It installs the additive Helm release through the reusable
`modules/mindeval-workshop` module. Its default local backend is suitable for
review; use the deployment's normal state-management workflow before applying.

Copy the non-secret values in `example.tfvars` into your own deployment settings,
replace the example image with the published workshop digest, and provide the
exact existing cluster context. Create the chart's referenced Secrets through
the established platform credential workflow first. No raw Token Factory or
attendee credentials are accepted here.

```bash
terraform -chdir=k8s-inference/examples/mindeval-workshop init -backend=false
terraform -chdir=k8s-inference/examples/mindeval-workshop validate
terraform -chdir=k8s-inference/examples/mindeval-workshop plan -var-file=your.tfvars
```

Review the single Helm release plan and install only after verifying the chart's
database/Gateway prerequisites. A live manual Helm installation must be adopted
into this module's state before Terraform manages the same release; do not
attempt to create a competing release with the same fixed service names.
