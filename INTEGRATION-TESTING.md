# Integration Testing & Cross-Repo Development

How to develop, test, and release **`steerable-framework`** alongside its three
downstream consumer repos:

- **[`deeppath`](https://github.com/deeppath/deeppath)** — Next.js / pnpm web app (uses `@steerable/agent-protocol` + `@steerable/agent-ui`)
- **[`deeppath-api`](https://github.com/deeppath/deeppath-api)** — FastAPI / uv backend (uses `steerable-agent-{protocol,harness,runtime}`)
- **[`deeppath-agent`](https://github.com/deeppath/deeppath-agent)** — Electron / pnpm desktop shell with embedded Python sidecar (uses `@steerable/agent-{protocol,harness}` + `steerable-sidecar` runtime)

> Companion document: [`RELEASING.md`](./RELEASING.md) covers the actual `bump_to.sh → tag → publish-{npm,pypi}.yml` pipeline. This document is about everything **before** that — local development, integration testing, and CI.

---

## TL;DR — pick the right mode for what you're doing

| You're changing… | Then in each consumer repo, run… |
|---|---|
| **Only downstream code** (framework untouched) | Nothing special. `pnpm install` / `uv sync --frozen` pulls 0.2.x from public registries. |
| **Framework + at least one downstream** (active integration) | `./scripts/use_framework_local.sh` (deeppath, deeppath-agent) or `./scripts/use_framework_source.sh` (deeppath-api) |
| **Validating a release candidate** before publishing | `./scripts/use_framework_wheels.sh` (deeppath-api) — TS side: bump to `file:` tarball manually |
| **Reverting back to public registry** | `./scripts/use_framework_npm.sh` (deeppath, deeppath-agent) or `./scripts/use_framework_pypi.sh` (deeppath-api) |

---

## The three resolution modes

Each downstream repo can resolve `steerable-*` packages in one of three ways. **The default — and the only one CI / Docker / production uses — is the public registry.** The other two are local-dev escape hatches.

### Mode A · Public registry (default)

`steerable-*` comes from **npmjs.org** (TS) and **pypi.org** (Py). Lockfile pins exact versions. This is what every fresh `git clone` resolves to and what every CI pipeline uses.

```bash
# deeppath / deeppath-agent
pnpm install --frozen-lockfile

# deeppath-api
uv sync --frozen
```

You're in this mode if `package.json` shows `"^0.2.0"` (not `"link:..."`) and `pyproject.toml` has **no** `[tool.uv.sources]` block for `steerable-*`.

### Mode B · Sibling source (editable)

`steerable-*` comes from a sibling `../steerable-framework` checkout via pnpm's `link:` mechanism (TS) or uv's `editable = true` paths (Py). Edits in the framework repo are picked up immediately by the consuming repo's tests / dev server — no rebuild needed.

Use when you're iterating on framework code AND its consumer in lockstep.

```bash
# In each consumer repo (assumes ../steerable-framework exists):
cd deeppath        && ./scripts/use_framework_local.sh
cd deeppath-api    && ./scripts/use_framework_source.sh
cd deeppath-agent  && ./scripts/use_framework_local.sh
```

To unwind: run the corresponding `*_npm.sh` / `*_pypi.sh` script.

### Mode C · Local wheels / tarballs (release-candidate validation)

`steerable-*` comes from artefacts pre-built into `../steerable-framework/dist/`. Used for "publish dry-run" — the framework version is what *would* go to PyPI / npm, but we install from local files first so we can validate before the actual `git push`.

Currently scripted only on the Python side:

```bash
cd steerable-framework
./scripts/release/build-local-artifacts.sh   # builds dist/py/*.whl + dist/npm/*.tgz

cd ../deeppath-api
./scripts/use_framework_wheels.sh            # installs from dist/py/*.whl
uv run pytest                                # final smoke
./scripts/use_framework_pypi.sh              # back to mode A
```

For TS (deeppath / deeppath-agent), the same idea works manually:

```bash
cd deeppath/apps/web
pnpm add "@steerable/agent-protocol@file:../../../steerable-framework/dist/npm/steerable-agent-protocol-0.3.0-rc.0.tgz"
```

---

## Three end-to-end scenarios

### Scenario 1 — Downstream-only change

You're adding a feature to `deeppath/apps/web` or fixing a bug in `deeppath-api/app/services/...` that doesn't touch the framework.

```bash
cd deeppath-api
git checkout -b feature/my-fix
# … edit code, run tests …
uv run pytest tests/test_my_fix.py
git commit -am "feat(api): add my fix"
git push
```

CI takes over from there. Nothing about steerable touches your workflow.

### Scenario 2 — Framework + downstream change

You're adding a new spec field that needs to flow through `agent-protocol`, get implemented in `agent-harness`, and consumed in `deeppath-api/app/services/harness/`.

```bash
# 1. Set both repos to "active development" mode
cd deeppath-api
./scripts/use_framework_source.sh   # uv now reads from ../steerable-framework

# 2. Iterate (file save in framework → instantly visible in api tests)
cd ../steerable-framework
git checkout -b feat/new-spec-field
# … add spec field, regenerate codegen, write framework tests …
pnpm gen && pnpm test                  # TS side
uv run pytest                          # Py side

cd ../deeppath-api
# … wire api to use the new field …
uv run pytest tests/test_harness_*.py  # framework changes hot-reload via editable install

# 3. When both green, commit each repo separately
cd ../steerable-framework
git commit -am "feat(protocol): add new spec field" && git push
# release.yml will auto-open a release PR for the framework

cd ../deeppath-api
./scripts/use_framework_pypi.sh        # revert to PyPI before committing
git commit -am "feat(api): consume new spec field"
# (api commit can wait until framework's new version is published, then bump uv.lock)
```

The same flow works for TS via `use_framework_local.sh` in deeppath / deeppath-agent.

### Scenario 3 — Cutting a new framework version

Operator-driven, lockstep, single command. See [`RELEASING.md`](./RELEASING.md) for the full pipeline; the short version:

```bash
cd steerable-framework
git switch main && git pull           # clean main, ahead-of-remote = 0

# 1. Bump all 7 packages + workspace root in one shot
./scripts/release/bump_to.sh 0.3.0    # script refreshes lockfiles + runs lockstep gate

# 2. Review + commit + tag
git diff
git add -A && git commit -m "chore(release): v0.3.0"
git tag v0.3.0

# 3. Push (this is the only step that can't be undone trivially)
git push origin main v0.3.0

# 4. Watch CI: validate (lockstep gate) → publish-npm → publish-pypi
gh run watch $(gh run list --workflow release.yml --limit 1 --json databaseId -q '.[0].databaseId')

# 5. Confirm registries (~3 minutes after push)
npm view @steerable/agent-ui@0.3.0 version
curl -s https://pypi.org/pypi/steerable-sidecar/0.3.0/json | jq -r '.info.version'

# 6. Bump the three downstream repos' lockfiles
cd ../deeppath        && pnpm update @steerable/agent-protocol @steerable/agent-ui
cd ../deeppath-agent  && pnpm update @steerable/agent-harness @steerable/agent-protocol
cd ../deeppath-api    && uv lock --upgrade-package steerable-agent-protocol \
                                 --upgrade-package steerable-agent-harness \
                                 --upgrade-package steerable-agent-runtime \
                                 --upgrade-package steerable-sidecar \
                       && uv sync
```

(`^0.2.0` / `>=0.2.0,<1.0.0` ranges in lockfiles auto-accept new 0.x minors, so `pnpm update` / `uv lock --upgrade-package` is enough — no `package.json` / `pyproject.toml` edit needed.)

**If the lockstep gate rejects the tag** (it will if `bump_to.sh` was skipped or partially applied — first happened with `v0.2.1`): nothing was published, no GitHub Release was created. Fix locally with `bump_to.sh`, commit, and tag a higher version (e.g. `v0.3.1`). The dead tag is harmless to leave in place as a historical marker.

---

## Per-repo cheat sheet

| Repo | Toggle to local-source | Toggle back to registry | Test command |
|---|---|---|---|
| `deeppath` | `./scripts/use_framework_local.sh` | `./scripts/use_framework_npm.sh` | `cd apps/web && pnpm type-check && pnpm test` |
| `deeppath-api` | `./scripts/use_framework_source.sh` | `./scripts/use_framework_pypi.sh` | `uv run pytest` |
| `deeppath-agent` | `./scripts/use_framework_local.sh` | `./scripts/use_framework_npm.sh` | `pnpm test` (vitest) |
| `steerable-framework` | n/a (it IS the source) | n/a | `pnpm test && uv run pytest` |

The `deeppath-api` scripts also have a third mode `./scripts/use_framework_wheels.sh` for installing from locally-built `.whl` files — useful for RC validation before PyPI publish.

---

## CI/CD pipeline overview

### `steerable-framework` (the producer)

```
            push to main                       push tag vX.Y.Z
                  │                                    │
                  ▼                                    ▼
   ┌──────────────────────────────┐           ┌────────────────┐
   │ ci.yml │ docs.yml │ storybook│           │   release.yml  │
   │ (ts/py │ (mkdocs  │ -quality │           │                │
   │ /side- │  + smoke │ (a11y +  │           │  validate job: │
   │ car/   │  + GH    │  VRT)    │           │   1. resolve   │
   │ examp- │  Pages)  │          │           │      version   │
   │ les/   │          │          │           │   2. lockstep  │
   │ codegen│          │          │           │      gate      │
   │ -idem) │          │          │           │   3. create    │
   └──────────────────────────────┘           │      Release   │
                                              └────────┬───────┘
                                                       │
                                          ┌────────────┴────────────┐
                                          ▼                         ▼
                                   publish-npm.yml           publish-pypi.yml
                                   (sigstore provenance)     (PyPI API token)
                                          │                         │
                                          ▼                         ▼
                              @steerable/agent-{protocol,    steerable-{agent-protocol,
                              harness,ui}@x.y.z              agent-harness,agent-runtime,
                                                             sidecar}==x.y.z
```

Triggers:
- `push: main` → ci, docs, storybook-quality
- `push: tags ['v*']` → release.yml (which chains publish-{npm,pypi}.yml via `workflow_call`)
- `workflow_dispatch` → manual override on every workflow (release.yml takes a `version:` input for re-runs against an existing tag)

### `deeppath` (consumer)

| Workflow | Trigger | What it does |
|---|---|---|
| `docker-build-push.yml` | push staging/feature, tag `v*`, PR | Builds the Next.js Docker image, builds desktop-web zip artefact (`pnpm install --frozen-lockfile`), pushes to Aliyun, SSH-deploys, pings Feishu |

`steerable-*` resolution: 100% from npm registry via `pnpm-lock.yaml`. No framework checkout in CI. Tailwind v4 `@source` reads from `node_modules/@steerable/agent-ui/{dist,src}` — works in Docker because pnpm hydrates node_modules from the lockfile.

### `deeppath-api` (consumer)

| Workflow | Trigger | What it does |
|---|---|---|
| `docker-build-push.yml` (jobs: `harness-regression`, `build-test`, `build-api`, `deploy-test`, `deploy`) | push staging/feature, tag `v*`, PR | `harness-regression` runs `uv sync --frozen --group dev` + targeted pytest gates; `build-*` builds & pushes API + WebSocket Docker images; `deploy-*` SSH-rolls compose stacks |

`steerable-*` resolution: PyPI registry via `uv.lock`. The Dockerfile currently runs `uv pip install --system .` from `pyproject.toml` only (not `uv.lock`); follow-up tracked in framework `TODO.md` to switch the image to `uv sync --frozen` for full lock-fidelity.

### `deeppath-agent` (consumer)

**No CI workflows yet.** Release builds happen locally via:

```bash
pnpm prepare:sidecar               # builds embedded CPython runtime — STILL needs sibling framework
pnpm build:{mac,win,linux}         # tsc + electron-builder
```

The sidecar build is the one place that can't yet run from public registry — `prepare-sidecar.sh` shells out to `../steerable-framework/packages/sidecar/build/build_sidecar.py --from-wheels`. Removing that dependency (and adding a GH Actions release workflow) is on the framework TODO under "Cross-platform sidecar build matrix".

---

## Common pitfalls

### "I `pnpm install` and `@steerable/agent-ui` shows version 0.2.0 but my framework changes aren't reflected"

You're in mode A (npm registry). The npm package is the published 0.2.0, not your local edit. Switch with `./scripts/use_framework_local.sh`.

### "I switched to local mode but Tailwind classes from the framework aren't generated"

Run `pnpm build` once inside `../steerable-framework/packages/agent-ui/ts/` so `dist/` exists — Tailwind's `@source` scans both `dist/` (built) and `src/` (jsx) and one of the two needs to actually have files.

### "`uv sync` says `editable install path doesn't exist`"

Your `pyproject.toml` has a `[tool.uv.sources]` override pointing at a sibling repo that's not there (e.g. you ran `use_framework_source.sh` then deleted `../steerable-framework`). Fix: `./scripts/use_framework_pypi.sh`.

### "I tagged `v0.3.0` but `release.yml` failed at the lockstep gate"

You forgot to run `./scripts/release/bump_to.sh 0.3.0` first, or only some packages got bumped. The gate refuses to publish unless **all 7 publishable packages** report exactly the tag version. Fix: `bump_to.sh 0.3.0` properly, commit, then either:
- re-tag the same version: `git tag -d v0.3.0 && git push --delete origin v0.3.0 && git tag v0.3.0 && git push origin main v0.3.0`, or
- bump to the next version (`v0.3.1`) and leave the dead tag in place.

Nothing was published from the failed tag (no GitHub Release was created either) — the gate runs **before** the release-creation step.

### "npm publish fails with `EOTP`"

The `NPM_TOKEN` GH secret is a granular access token with token-level 2FA enabled. Either:
1. Account-level 2FA must be set to "Authorization only" (not "and writes")
2. Or replace `NPM_TOKEN` with a Classic Automation Token (which bypasses 2FA by spec)

See `RELEASING.md` for the full sequence.

### "PyPI publish leaks my API token in chat"

Don't paste tokens. Always use `gh secret set <NAME> --repo <repo>` and paste at the prompt, then Ctrl-D. Better: migrate to PyPI Trusted Publishing (no token at all) — see `TODO.md → Migrate PyPI auth to Trusted Publishing`.

---

## Verification commands (sanity check after a switch)

After running any `use_framework_*.sh` script, confirm the change took effect:

```bash
# deeppath / deeppath-agent (TS side)
node -p 'require("./apps/web/package.json").dependencies["@steerable/agent-protocol"]'
# expect either '^0.2.0' (npm mode) or 'link:...' (local mode)

# deeppath-api (Py side)
grep -A 3 '\[tool.uv.sources\]' pyproject.toml || echo "(no overrides — PyPI mode)"
uv run python -c 'import steerable_agent_harness as m; print(m.__file__)'
# expect either ".venv/site-packages/..." (PyPI/wheel mode)
# or "../steerable-framework/packages/agent-harness/py/src/..." (source mode)
```

---

## Future improvements

Tracked in [`TODO.md`](./TODO.md):

- Cross-platform sidecar build matrix (currently desktop release builds are local-only)
- `build_sidecar.py` should support pip-installing `steerable-*` from PyPI directly, removing the last "needs sibling checkout" step in `deeppath-agent`
- Migrate PyPI auth to Trusted Publishing
- ~~Reconcile release-please component bumps with the ts↔py lockstep validator~~ (done — release-please replaced with tag-driven lockstep flow, May 2026)
- `deeppath-api/Dockerfile` should `COPY uv.lock` and use `uv sync --frozen` for full lock-fidelity in production images
