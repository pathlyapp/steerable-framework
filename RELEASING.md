# Releasing the Steerable Framework

This document covers two release modes:

- **Mode B — Local artifacts** (no credentials needed): build tarballs/wheels
  into `dist/` and consume them by file path. Ideal for dogfooding inside the
  DeepPath ecosystem before going public.
- **Mode C — Public publish**: push to npm and PyPI under `@steerable/*` and
  `steerable-*`. Requires registry credentials and the GitHub Actions
  workflow configured below.

## Packages in scope

| Tier | Package | Output |
| --- | --- | --- |
| 1 | `@steerable/agent-protocol` | `dist/npm/steerable-agent-protocol-X.Y.Z.tgz` |
| 1 | `steerable-agent-protocol` (Py) | `dist/py/steerable_agent_protocol-X.Y.Z-*.whl` + sdist |
| 2 | `@steerable/agent-harness` | `dist/npm/steerable-agent-harness-X.Y.Z.tgz` |
| 2 | `steerable-agent-harness` (Py) | `dist/py/steerable_agent_harness-X.Y.Z-*.whl` + sdist |
| 3 | `steerable-agent-runtime` (Py) | `dist/py/steerable_agent_runtime-X.Y.Z-*.whl` + sdist |
| 3 | `steerable-sidecar` (Py) | `dist/py/steerable_sidecar-X.Y.Z-*.whl` + sdist |
| 4 | `@steerable/agent-ui` | `dist/npm/steerable-agent-ui-X.Y.Z.tgz` |

The protocol packages use **lock-step versioning** (npm + PyPI must agree),
enforced by `scripts/check_lockstep_versions.py` in CI.

---

## Mode B — Build local artifacts

### One-shot script

```bash
make release-local
# Or, expanded:
./scripts/release/build-local-artifacts.sh
```

Both wrappers run the steps below; pick whichever style fits your shell.

### Step-by-step

```bash
cd /path/to/steerable-framework

# 0. Clean install + sync workspaces
pnpm install --frozen-lockfile
uv sync --all-packages

# 1. Regenerate codegen + verify drift
pnpm gen
pnpm check:drift
uv run python scripts/generate_py.py
uv run python scripts/check_drift.py

# 2. Run the full test suite (must be green before packaging)
pnpm -r test
uv run pytest -q

# 3. Build TS packages
pnpm -r --filter '@steerable/*' build

# 4. Pack TS tarballs into dist/npm/
rm -rf dist && mkdir -p dist/npm dist/py
for pkg in agent-protocol agent-harness agent-ui; do
  (cd packages/$pkg/ts && \
   pnpm pack --pack-destination "$PWD/../../../dist/npm")
done

# 5. Build Python wheels + sdists into dist/py/
uv build --all-packages --out-dir dist/py
# Drop example wheels from publishable output:
rm dist/py/steerable_example_*.{whl,tar.gz} 2>/dev/null || true

# 6. Smoke-test from a clean Node + Python environment
./scripts/release/verify-local-artifacts.sh
```

### Smoke-test verification

The verification script (or manual equivalent below) installs the artifacts
into throwaway environments and runs minimal real code.

```bash
# Python
uv venv /tmp/steerable-verify-py --python 3.12
uv pip install --python /tmp/steerable-verify-py/bin/python \
    dist/py/steerable_agent_protocol-*.whl \
    dist/py/steerable_agent_harness-*.whl \
    dist/py/steerable_agent_runtime-*.whl \
    dist/py/steerable_sidecar-*.whl
/tmp/steerable-verify-py/bin/python -c "
import asyncio
from steerable_agent_protocol import ToolCall, SSEEvent
from steerable_agent_runtime import ToolRouter, tool
router = ToolRouter()
@tool(router=router)
async def hello() -> str:  return 'hi'
print(asyncio.run(router.dispatch(ToolCall(id='c1', name='hello', arguments={}))))
"

# Node ESM
mkdir /tmp/steerable-verify-npm && cd /tmp/steerable-verify-npm
cat > package.json <<JSON
{
  "type": "module",
  "dependencies": {
    "@steerable/agent-protocol": "file:$REPO/dist/npm/steerable-agent-protocol-0.1.0.tgz",
    "@steerable/agent-harness":  "file:$REPO/dist/npm/steerable-agent-harness-0.1.0.tgz"
  },
  "pnpm": {
    "overrides": {
      "@steerable/agent-protocol": "file:$REPO/dist/npm/steerable-agent-protocol-0.1.0.tgz",
      "@steerable/agent-harness":  "file:$REPO/dist/npm/steerable-agent-harness-0.1.0.tgz"
    }
  }
}
JSON
pnpm install --no-frozen-lockfile
node --input-type=module -e "
import { decideToolMode, isTerminalResult } from '@steerable/agent-harness';
console.log({mode: decideToolMode('read_file'), done: isTerminalResult({success:true,terminal:true})});
"
```

> **Why `pnpm.overrides`?** Tarballs of internal packages declare each other as
> registry dependencies (`workspace:*` is rewritten on `pnpm pack`). Without
> overrides, pnpm tries to fetch them from `registry.npmjs.org` and fails.

### Consuming Mode B artifacts from another project

#### TypeScript (Next.js / Vite / Electron)

```jsonc
{
  "dependencies": {
    "@steerable/agent-protocol": "file:../steerable-framework/dist/npm/steerable-agent-protocol-0.1.0.tgz",
    "@steerable/agent-ui":       "file:../steerable-framework/dist/npm/steerable-agent-ui-0.1.0.tgz"
  },
  "pnpm": {
    "overrides": {
      "@steerable/agent-protocol": "file:../steerable-framework/dist/npm/steerable-agent-protocol-0.1.0.tgz"
    }
  }
}
```

#### Python (FastAPI / Sidecar embedder)

```bash
uv add /path/to/dist/py/steerable_agent_protocol-0.1.0-py3-none-any.whl
uv add /path/to/dist/py/steerable_agent_harness-0.1.0-py3-none-any.whl
uv add /path/to/dist/py/steerable_agent_runtime-0.1.0-py3-none-any.whl
```

`uv add` writes a SHA256 to `uv.lock`, so you get reproducible installs even
without a registry.

---

## Mode C — Public publish

### Prerequisites (one-time)

1. **Reserve the names**:
   - [x] npm scope `@steerable` — already registered (May 2026). All four
         publishable names (`agent-protocol`, `agent-harness`, `agent-ui`)
         return HTTP 404 on the registry, confirming the slot is free.
   - [ ] PyPI project names `steerable-agent-protocol`,
         `steerable-agent-harness`, `steerable-agent-runtime`,
         `steerable-sidecar`. See "Configuring PyPI publish" below.

2. **Configure `NPM_TOKEN`**:
   - Generate a **Granular Access Token** (recommended over the legacy
     "automation" type) at
     <https://www.npmjs.com/settings/~/tokens>:
     - **Permissions** → "Read and write"
     - **Packages and scopes** → restrict to the `@steerable` scope
     - Expiry: pick something ≥1 year so release-please doesn't break on
       silent expiry; rotate by setting a new token before the old expires.
   - Drop it into the repo:

     ```bash
     gh secret set NPM_TOKEN \
       --repo pathlyapp/steerable-framework \
       --body "<paste-the-token>"
     ```

3. **Configure PyPI publish (pick one)**:

   - **(A — recommended) Trusted Publishing**, no long-lived secret:
     - Go to <https://pypi.org/manage/account/publishing/> for each
       project (or the umbrella account if all four projects share an
       owner).
     - Add a publisher: owner `pathlyapp`, repo `steerable-framework`,
       workflow `publish-pypi.yml`, environment `pypi`.
     - In the GitHub repo, create an Environment named `pypi`
       (Settings → Environments → New environment) and (optionally) gate
       it on a maintainer-approval rule.
   - **(B) API token**:

     ```bash
     # On https://pypi.org/manage/account/token/, scope it to all four
     # `steerable-*` projects.
     gh secret set PYPI_API_TOKEN \
       --repo pathlyapp/steerable-framework \
       --body "pypi-XXXX..."
     ```

   `publish-pypi.yml` auto-detects which mode is active (presence of
   `PYPI_API_TOKEN` selects mode B, otherwise OIDC / Trusted Publishing).
   Until either is configured the workflow short-circuits with a clear
   notice — it will not fail the release.

4. **Pipeline (already wired, no action needed)**:

   ```text
   push to main (with conventional-commit messages)
        │
        ▼
   release.yml  ── release-please opens / updates a "release PR"
        │
        │ (maintainer reviews + merges the release PR)
        ▼
   release.yml  ── release-please cuts per-package tags + GitHub Releases
        │
        ├─► publish-npm.yml   triggered by `release: published`
        │       └─ skips packages whose version is already on registry
        │
        └─► publish-pypi.yml  triggered by `release: published`
                └─ same idempotent skip logic against PyPI's JSON API
   ```

   `publish-{npm,pypi}.yml` are idempotent: re-dispatching them after a
   partial failure only re-publishes packages whose local version is still
   missing upstream.

### Manual publish (escape hatch)

If the automated release-please pipeline is broken, you can publish from a
clean checkout of the tagged commit:

```bash
# TS — npm
pnpm install --frozen-lockfile
pnpm gen
pnpm -r --filter '@steerable/*' build
NODE_AUTH_TOKEN="$NPM_TOKEN" \
  pnpm -r --filter '@steerable/*' publish --no-git-checks

# Py — PyPI
uv build --all-packages --out-dir dist/py
rm dist/py/steerable_example_*.{whl,tar.gz} 2>/dev/null || true
uv publish --token "$PYPI_API_TOKEN" dist/py/steerable_*.whl dist/py/steerable_*.tar.gz
```

### Versioning rules

- **Lock-step** for `agent-protocol` (TS+Py): a `feat:` touching `spec/` bumps
  both SDKs together. Enforced by `scripts/check_lockstep_versions.py`.
- **Independent** for `agent-harness`, `agent-runtime`, `agent-ui`, `sidecar`:
  release-please tracks each per its own conventional-commit subdirectory.
- Pre-1.0: minor (`0.X`) is the breaking-change axis. **Don't ship 1.0 until
  the public API is stable for at least one minor release cycle.**

---

## Common pitfalls

- **Forgetting `pnpm gen` before packing** — drift checker will fire in CI but
  not in local pack. Always `pnpm gen && pnpm check:drift` first.
- **Stale `uv.lock`** — `uv build` doesn't re-resolve. If you bumped a
  workspace package, run `uv sync --all-packages` first.
- **`additionalProperties: false` regressions** — some envelopes are
  intentionally open (`SSEEvent`, `ChatMessage`); some are intentionally
  closed (`ToolCall`, `SidecarRequest`). Don't flip these without a major bump.
- **Examples leaking into releases** — `dist/py/steerable_example_*` are
  built by `uv build --all-packages` but **must not** be published. The local
  build script removes them; the publish workflow does too.
- **Missing `.js` extensions in TS source** — every relative `import`/`export`
  in TypeScript source must end in `.js` (yes, even though the source is
  `.ts`). Required for Node ESM. The codegen template enforces it; new
  hand-authored modules must follow suit.

---

## Release checklist

Copy this into the release PR description:

- [ ] `pnpm install --frozen-lockfile` ✓
- [ ] `uv sync --all-packages` ✓
- [ ] `pnpm gen && pnpm check:drift` ✓
- [ ] `uv run python scripts/generate_py.py && uv run python scripts/check_drift.py` ✓
- [ ] `python scripts/check_lockstep_versions.py` ✓ (Python ≥ 3.11)
- [ ] `pnpm -r test` ✓ (44 + 2 = 46 passing)
- [ ] `uv run pytest -q` ✓ (105 passing)
- [ ] `examples/py-minimal`, `examples/ts-minimal`, `examples/sidecar-roundtrip` all run
- [ ] Mode B verification (`./scripts/release/verify-local-artifacts.sh`) ✓
- [ ] CHANGELOG entries via Changesets / release-please ✓
- [ ] Lockfiles refreshed in three downstream repos ✓ (see [`docs/migration/deeppath.md`](./docs/migration/deeppath.md))
