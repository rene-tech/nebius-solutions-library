# Important customer incident emails

Scientific AI uses the existing Prometheus Operator and Alertmanager, not an
agent polling customer jobs or a second notification service. Terminal operation
metrics come from the durable usage ledger; an HTTP 202 is not success.

The October 7 deployment sends to **rene@nebius.com**, as requested by the owner.
It does not post to Slack or contact customers. Existing public-website alerts
are separate and unchanged.

## What sends an email

| Condition | Threshold |
| --- | --- |
| Scientific customer job stops with failed/expired/preempted terminal status | It had executed for at least 5 minutes; detection holds for 1 minute |
| Repeated terminal customer failures | At least 5 in 10 minutes; detection holds for 2 minutes |
| No healthy API monitoring targets | 5 minutes; email distinguishes an outage from a monitoring-path failure |
| A PostgreSQL instance remains unready | 30 minutes |

Internal `system`, `system-*`, `demo` and `demo-user` tenants are excluded from
customer-failure rules. Successful retry attempts, explicit cancellations,
ordinary queue/capacity waits and rejected admission validation do not page.
The long-job rule counts terminal operation facts, not failed attempts that
recover. Its five-minute duration starts at the operation's recorded start;
it is not a measure of GPU simulation time.

Alertmanager groups by alert, tenant and model, waits one minute initially,
groups changes over ten minutes, and repeats a still-firing group at most once
per 24 hours. Resolution emails are disabled. Different important incidents
can still produce separate emails. This is not a blanket one-email/day limit.

Emails contain the model/tenant and an admin link, not payloads, API keys or
presigned artifact URLs. Open **Apps → Runs** to find the operation ID, failure
reason, attempts and resumable checkpoint. The alerts never resubmit a job.

## Terraform configuration

In the existing `deployment` object in `terraform.tfvars`, configure:

```hcl
observability = {
  alertmanager = {
    enabled = true
    email = {
      enabled         = true
      to              = "operator@example.com"
      from            = "alerts@your-verified-domain.example"
      smarthost       = "smtp.resend.com:587"
      username        = "resend"
      password_secret = "fs2-important-alert-mail"
      password_key    = "smtp-password"
      admin_url       = "https://your-platform.example/admin"
    }
  }
}
```

Provision the named Secret in `fs2-observability` through the deployment's
credential handoff. Neither the SMTP password nor an API token belongs in
tfvars, Helm values, Git or the email. The foundation stage installs the
`charts/addons/important-alerts` chart and assigns the exact monitoring release
label required by Prometheus's `ruleSelector`.

For this existing cluster, the already-authorized Resend sending credential was
copied from the website integration into the dedicated alert Secret without
printing it. The owner account's allowed recipient is `rene@nebius.com`.
`onboarding@resend.dev` is suitable for that authorized recipient, not a promise
of arbitrary-recipient delivery; use a verified sender for another deployment.

## Verification and boundaries

Check all three layers: the four rules are **loaded and healthy** in Prometheus,
Alertmanager has the important-only route, and the SMTP provider accepts a
clearly labelled test email. Unit rule evaluation alone proves none of delivery.

October 7: the labelled delivery-proof check observed the email-send counter
increase from 15 to 16 with zero email-send failures. It later read 17. This is
a shared counter across email receivers, not a per-recipient delivery receipt
or proof of an exact number of test messages. The earlier timestamped probe and
the final proof alert are both inactive; no recurring synthetic test is installed.
The send-only Resend credential cannot read message delivery events. SMTP
acceptance is verified, recipient inbox receipt is not independently asserted.

The operation window is ten minutes; this does not retroactively notify about
old failures predating deployment. Readiness checks do not detect every silent
scientific error or stalled job. The database rule detects unready database
Pods, not loss of an entirely missing replica or all possible replication lag.
These alerts are an actionable incident channel, not a replacement for the
customer-shaped acceptance policy or complete service SLO coverage.

See [the recovery acceptance](../acceptance/reliability-recovery-20261007/README.md)
for the exact deployment and fault-injection evidence.
