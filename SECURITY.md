# Security Policy

## Supported versions

Steerable is pre-1.0; only the **latest minor** receives security fixes.

| Version | Supported |
|---|---|
| 0.2.x   | ✅ |
| 0.1.x   | ❌ (please upgrade) |
| < 0.1   | ❌ |

After 1.0, the policy will widen to "latest two minors".

## Reporting a vulnerability

**Please do not open a public GitHub issue for security problems.**

Email <security@pathlyapp.com> with:

- A description of the issue and which package(s) are affected
  (`@steerable/agent-protocol`, `steerable-sidecar`, etc.).
- Steps to reproduce, or a minimal proof-of-concept.
- Your assessment of severity and any suggested mitigation.

You can encrypt sensitive content using the maintainers' age key:
[`docs/security/age-public-key.txt`](./docs/security/age-public-key.txt) (to be
published; until then, plain email is acceptable).

We will acknowledge receipt within **3 business days** and aim to ship a fix
or mitigation within **30 days** for critical issues.

## Scope

In scope:

- Code in this repository (all `packages/*`, `scripts/*`, `.github/workflows/*`).
- Published artefacts on npm under the `@steerable/*` scope and on PyPI under
  the `steerable-*` prefix.
- The portable Python sidecar binary built by `packages/sidecar/build/build_sidecar.py`.

Out of scope:

- Vulnerabilities in upstream dependencies (`pydantic`, `astral-sh/python-build-standalone`,
  `react`, `tailwindcss`, …) — please report directly to the upstream maintainers.
- Vulnerabilities in downstream consumer applications (DeepPath et al.) —
  report to those projects directly.
- Issues that require a malicious sibling-source checkout (Mode B / `use_framework_local.sh`),
  which is a developer-only workflow and assumes a trusted local environment.

## Supply-chain integrity

Every `@steerable/*` npm tarball ships **sigstore provenance attestations**
(via npm `--provenance`). Verify with:

```bash
npm audit signatures @steerable/agent-ui
```

PyPI uploads currently use an account-scoped API token; migration to PyPI
**Trusted Publishing** (OIDC, no long-lived secret) is tracked in
[`TODO.md`](./TODO.md).

The sidecar binary is built reproducibly from `python-build-standalone` and
code-signed (macOS notarised, Windows Authenticode) via the
`sidecar-codesign.yml` workflow.

## Hall of fame

Researchers who report valid vulnerabilities will (with their consent) be
credited here once the fix ships.

_(empty — be the first.)_
