# Security Policy

Tera Pilot is a local-first, vendor-neutral coding agent. Its threat model and
security boundaries are documented in
[`THREAT_MODEL.md`](THREAT_MODEL.md) — please read it before reporting.

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Instead, report privately via GitHub's private vulnerability reporting:

1. Open the repository's **Security** tab:
   <https://github.com/ilyaosovskoi/tera-pilot/security/advisories>
2. Click **"Report a vulnerability"** and fill in the details.

If you cannot use private reporting, email the maintainers — see the repository
description / commit metadata for the contact address.

### What to include

- Affected version(s) and commit hash, if known;
- a minimal reproduction (command, input, environment);
- impact assessment — what an attacker could gain;
- whether you have a proposed fix.

You can expect an acknowledgement within a few days and a coordinated fix
timeline. We will credit you in the advisory unless you prefer to stay anonymous.

## Supported versions

Only the latest release on `main` is actively supported for security fixes.
We aim to backport critical fixes when practical.

## Security scope

The following are **not** vulnerabilities:

- Data sent to cloud LLM providers by design (local-first ≠ always-offline);
- missing enterprise features that are explicitly roadmap items (SSO/RBAC,
  centralized policy distribution, formal certifications);
- optional Rust acceleration being absent (pure-Python fallbacks are the
  supported default path).

## Claims discipline

Per the product strategy, Tera Pilot does not claim to be vulnerability-free,
enterprise-ready, or "100% offline". If you see such a claim in code or docs,
that is a documentation bug — please report it under this policy.

## Dependency alerts

We monitor GitHub Dependabot alerts on the default branch. If you find a
dependency issue not covered by an alert, report it privately.
