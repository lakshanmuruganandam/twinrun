# Security policy

TwinRun runs code from pull requests, so the sandbox is the most security-sensitive part of the
project. See [README → Sandbox](README.md#sandbox) for how it is built and what it covers.

## Reporting a vulnerability

Please **do not open a public issue** for a sandbox escape or any other vulnerability.
Report it privately through GitHub's
[private vulnerability reporting](https://github.com/lakshanmuruganandam/twinrun/security/advisories/new).
Include a minimal repro if you can. You will get a reply within 7 days.

## Scope

In scope: escaping the worker sandbox (network access, writing outside the worker temp dir, starting
processes), making the webhook server run code from a PR, bypassing webhook signature checks, and
leaking the app's private key or installation tokens.

Out of scope: denial of service by a PR author against their own repository's check, and findings
that need an already-compromised host.

## Running TwinRun safely

- Run the server as an unprivileged user on a machine with no secrets beyond the app's own key.
- On Linux, prefer hosts that allow unprivileged user namespaces, so the OS network sandbox
  (`unshare -rn`) is active. The report shows which sandbox was used.
- Keep `.env` and the `.pem` file out of version control (the repo's `.gitignore` already does this).
