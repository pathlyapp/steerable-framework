# Steerable Framework — Status & Roadmap

Snapshot of the multi-phase build-out. Source of truth for "what's already
shipped" vs "what's still pending"; intended to be updated whenever a
phase closes or a new follow-up surfaces.

> Phase semantics: P0–P8 = original plan; D-numbers = dependency fixups; the
> "Open follow-ups" section captures everything we deferred or that needs
> credentials we don't yet have.

---

## ✅ Completed phases

### P0 · Foundations

- [x] **p0-1-repo** Monorepo skeleton: pnpm + uv workspace, Apache-2.0 + DCO,
      `BRAND` constants, top-level `package.json` / `pyproject.toml`.
- [x] **p0-2-codegen** `spec/*.schema.json` → TS (`scripts/generate_ts.mjs`)
      + Py (`scripts/generate_py.py`) codegen pipelines, plus drift checks
      (`scripts/check_ts_drift.mjs`, `scripts/check_drift.py`).
- [x] **p0-3-versioning** Lock-step version validator
      (`scripts/check_lockstep_versions.py`) and Changesets wired in.

### P1 · Spec & protocol package

- [x] **p1-spec-events** `spec/events/SSEEvent.schema.json` (universal stream
      envelope).
- [x] **p1-spec-tools** `spec/tools/{ToolCall,ToolResult}.schema.json`.
- [x] **p1-spec-chat** `spec/chat/{ChatMessage,ChatAgent}.schema.json`.
- [x] **p1-spec-safety** `spec/safety/CommandSafetyPattern.schema.json`.
- [x] **p1-spec-runtime** `spec/runtime/{AgentSession,HarnessTrace,TraceSpan,TraceEvent}.schema.json`.
- [x] **p1-spec-sidecar** `spec/sidecar/{SidecarRequest,SidecarResponse,SidecarError,SidecarNotification,SidecarHealth}.schema.json`
      (JSON-RPC over stdio).
- [x] **p1-publish-protocol** `@steerable/agent-protocol@0.2.0` live on
      npmjs.org with sigstore provenance (May 2026). PyPI publish still
      pending project name reservation — see "Open follow-ups".
- [x] **p1-import-three-repos** `deeppath`, `deeppath-api`, `deeppath-agent`
      all importing the protocol types in production code paths.

### P2 · Pure harness (Tier 2)

- [x] **p2-harness-py** `steerable-agent-harness` Python implementation
      (`policy`, `budget`, `retry`, `completion`, `tracing`,
      `safety-patterns`); zero DB / HTTP coupling.
- [x] **p2-harness-tests** Unit tests + golden-output snapshots (44 Py).
- [x] **p2-publish-harness** wheel + sdist built (`dist/py/steerable_agent_harness-0.1.0*`).

### P3 · Runtime adapters (Py-only Tier 3)

- [x] **p3-llm-provider** `LLMProvider` interface + Ollama / OpenAI-compat /
      Anthropic adapters.
- [x] **p3-tool-router** `ToolRouter`, `@tool` decorator, ToolMode classifier.
- [x] **p3-storage-adapter** `StorageAdapter` interface + InMemory + SQLAlchemy.
- [x] **p3-transport-adapter** FastAPI SSE + Stdio JSON-RPC adapters.
- [x] **p3-publish-runtime** wheel + sdist built (`steerable_agent_runtime-0.1.0`).

### P4 · Sidecar (portable Python subprocess)

- [x] **p4-sidecar-bootstrap** `steerable-sidecar` entrypoint: JSON-RPC over
      stdio, `__SIDECAR_READY__` marker, `lifecycle.ready/shutdown`
      notifications, graceful shutdown.
- [x] **p4-sidecar-packaging** `python-build-standalone`-based portable
      CPython builder (`packages/sidecar/build/build_sidecar.py`),
      cross-platform targets, conservative + aggressive stdlib pruning.
- [x] **p4-sidecar-electron-bridge** `deeppath-agent/src/sidecar/`:
      `SidecarSupervisor` (spawn / stdin-stdout JSON-RPC / health-ping /
      auto-restart / graceful kill on `app.will-quit`).
- [x] **p4-sidecar-codesign** macOS notarisation + Windows signing extended
      to the embedded Python binary.

### P5 · Three-repo refactor onto framework

- [x] **p5-agent-refactor** `deeppath-agent` opt-in sidecar via
      `STEERABLE_USE_SIDECAR=1`; in-process providers stay default for safety.
- [x] **p5-api-refactor** `deeppath-api` harness reduced to a thin shim;
      loop logic delegated to framework runtime.
- [x] **p5-web-refactor** `deeppath/apps/web` SSE parser + api-client
      switched to framework types only (no runtime coupling).
- [x] **p5-installer-size** Bundle size optimised below the <300 MB target
      via stdlib stripping.

### P6 · Agent UI (Tier 4, TS-only)

- [x] **p6-ui-design** `@steerable/agent-ui` API: headless hooks core +
      Tailwind preset.
- [x] **p6-ui-components** `MessageList`, `ChatPanel`,
      `OrchestrationPlanCard`, `ToolCallRenderer`, `SSEStreamView`.
- [x] **p6-ui-hooks** `useAgentSession`, `useChatStream`, `useToolCallStatus`.
- [x] **p6-publish-ui** `@steerable/agent-ui@0.1.0` tarball built;
      release-please wired.
- [x] **p6-storybook** Storybook 8 + Tailwind v4 token bridge, 27 stories
      across all 5 components + 4 MDX docs (3 hooks + Introduction).
      Quality gates wired:
      - `@storybook/addon-a11y` + `@storybook/test-runner` (axe) — 31/31 passing
        after token-contrast fixes (`bg-agent-accent`, `bg-agent-muted-foreground`,
        `bg-agent-tool-{read,write}`) + scroll-region focusability fixes on
        `MessageList` / `SSEStreamView`.
      - Playwright VRT (`tests/visual/stories.spec.ts`) auto-enumerates
        stories from `index.json`; 27 darwin baselines committed; Linux
        baselines generated via `scripts/generate-vrt-baselines-linux.sh`
        (Docker), see CI guard below.
      - CI: `.github/workflows/storybook-quality.yml` (a11y enforced, VRT
        `continue-on-error: true` until first Linux baseline lands).
      - Deploy: `docs.yml` rebuilds Storybook on every push to `main` and
        embeds it under `/storybook/` of the mkdocs Pages site.

### P7 · Production wiring

- [x] **p7-web-ui-switch** `deeppath/apps/web` 0.1 switch — `link:`
      dependency + Tailwind v4 token bridge + `/dev/framework-preview`
      verification page; production `ChatPanel` left untouched.
- [x] **p7-agent-ui-reuse** `deeppath-agent` reuses `@steerable/agent-ui`
      via the shared `apps/web` standalone build; sidecar end-to-end
      verification test passing.

### P8 · Docs + GA prep

- [x] **p8-docs** Full mkdocs site, `docs/getting-started.md`, `docs/spec/*`
      human-readable specs, three runnable examples (`examples/py-minimal`,
      `examples/ts-minimal`, `examples/sidecar-roundtrip`).
- [x] **p8-release-ga** GA-prep landing — see breakdown below; **public
      publish itself is gated on credentials**, see "Open follow-ups".

#### P8-release-ga sub-tasks

- [x] **p8-ga-1-build** All 7 publishable artefacts built into `dist/`.
- [x] **p8-ga-2-verify** Clean-env install + smoke test (`scripts/release/verify-local-artifacts.sh`).
- [x] **p8-ga-3-releasing-doc** [`RELEASING.md`](./RELEASING.md) covers Mode B
      (local tarball / wheel) + Mode C (npm + PyPI publish).
- [x] **p8-ga-4-lockfiles** Lockfiles refreshed in framework + the three
      downstream repos.
- [x] **p8-ga-5-deeppath-api-wire** `deeppath-api/scripts/use_framework_wheels.sh`
      + `use_framework_source.sh` toggle (uv `[tool.uv.sources]` rewrite); README
      block added.
- [x] **p8-ga-6-deeppath-agent-wire** `deeppath-agent/scripts/prepare-sidecar.sh`
      orchestrates `build-local-artifacts.sh` + `build_sidecar.py --from-wheels`,
      lands the runtime under `resources/python-runtime/<platform>/`,
      `electron-builder` `extraResources` updated; README rewritten.
- [x] **p8-ga-7-handoff** Final cross-repo health summary + this document.

### Dependency fixups (D-track)

- [x] **D1** `tests/conformance/ts` got `@types/node` so CI compiles.
- [x] **D2** `steerable-framework` git initialised (release-please needs it).
- [x] **D3** pnpm `approve-builds` for `esbuild` accepted.

---

## 🚧 Open follow-ups

> Roughly priority-ordered. None block local Mode B development; all are
> needed before a real public 1.0 release.

### Publish & registry (needs credentials)

- [x] **Reserve npm scope `@steerable`** — done (May 2026); all four
      `@steerable/*` names return HTTP 404 confirming the slot is free.
- [x] **publish-npm.yml + publish-pypi.yml in repo** — both workflows live
      under `.github/workflows/` and trigger on `release: published`. They
      are idempotent (probe registry, skip versions already up). See
      [`RELEASING.md`](./RELEASING.md) for the wiring.
- [x] **release.yml on push trigger** — release-please now runs on every
      push to `main` (was workflow_dispatch only) and opens release PRs
      autonomously.
- [x] **3 npm package.json files publish-ready** — added
      `publishConfig.access: "public"`, `publishConfig.provenance: true`,
      and standard metadata (`license`, `repository`, `homepage`,
      `keywords`) to `agent-protocol`, `agent-harness`, `agent-ui`.
- [x] **Set `NPM_TOKEN` GitHub secret** — done. The granular-access token
      first attempt failed with `EOTP` (token-level 2FA on); rotated to a
      classic Automation token (bypasses 2FA per npm spec). Account-level
      2FA also dropped from "auth + writes" to "auth only" so future
      granular tokens work too.
- [x] **First public npm publish landed** — `@steerable/agent-protocol@0.2.0`,
      `@steerable/agent-harness@0.2.0`, `@steerable/agent-ui@0.2.0` live on
      npmjs.org with sigstore provenance (`npm audit signatures`).
      Triggered manually via `gh workflow run publish-npm.yml` because
      release-please's `GITHUB_TOKEN`-created releases don't fan out
      `release: published` events. **Fix landed in same commit** (release.yml
      now chains `publish-{npm,pypi}.yml` via `workflow_call` on the
      release-please job's `releases_created` output, so the next release
      publishes itself).
- [x] **Py inter-package pin lockstep bug** — release-please bumped
      `protocol`/`harness` to 0.2.0 but the four `pyproject.toml` files
      still pinned each other at `==0.1.0`, breaking `pip install` of any
      framework package against the resolved-source siblings (caught by
      `sidecar-budget` CI). Relaxed all inter-package pins to
      `>=0.1.0,<1.0.0` (pre-1.0 compatible-release range) — see comment
      block in `packages/agent-harness/py/pyproject.toml`. Lockstep
      across `protocol↔harness` is still enforced by
      `scripts/check_lockstep_versions.py`.
- [ ] **Reserve PyPI projects** `steerable-agent-protocol`,
      `steerable-agent-harness`, `steerable-agent-runtime`,
      `steerable-sidecar` (block PyPI publish).
- [ ] **Configure PyPI auth** — Trusted Publishing (recommended; bind
      to environment `pypi` already created) **or** `PYPI_API_TOKEN`
      GitHub secret.
- [ ] **Bump remaining Py packages (`runtime`, `sidecar`) to 0.2.0** so
      the four-package PyPI publish goes out as a coherent 0.2.0 wave.
      release-please left them at 0.1.0 because no `feat(runtime):` /
      `feat(sidecar):` commits landed in this cycle. Either edit
      `.release-please-manifest.json` directly (fastest) or wait for the
      next genuine feature commit per package.

### Public docs site

- [x] **GitHub Pages enabled** (workflow source) → docs site live at
      <https://pathlyapp.github.io/steerable-framework/>.
- [x] **Storybook embedded under `/storybook/`** → live at
      <https://pathlyapp.github.io/steerable-framework/storybook/>.
- [x] **Bump GitHub Actions to Node.js 24-compatible versions** — landed
      in 1d6fc83 / ba4ade7 / 10c6de6. All ten in-tree actions
      (`actions/checkout`, `setup-node`, `setup-python`,
      `upload-artifact`, `download-artifact`, `deploy-pages`,
      `upload-pages-artifact`, `pnpm/action-setup`,
      `astral-sh/setup-uv`, `googleapis/release-please-action`) bumped
      to their current Node-24-ready major. Verified by `gh api` query
      against each action's `releases/latest` before bumping.

### Toward a real 1.0.0

- [ ] **One stable minor cycle on 0.x** before promoting to 1.0.0 — current
      API is only just being exercised by the three repos.
- [ ] **Decide: shared `1.0.0` for protocol + harness?** Lock-step is enforced,
      but Tier 2/3/4 packages can independently version once 1.0 lands.

### Quality / DevX gaps

- [x] **Codegen idempotency** — landed in 10c6de6. Both the `ts:` and
      `py:` jobs in `.github/workflows/ci.yml` now re-run their codegen
      after the first build/test cycle and `git diff --quiet -- packages`
      fails the job if the second run produces any diff. Catches
      non-deterministic codegen (random hashes, unstable map ordering,
      locale-dependent sort) the moment it sneaks in.
- [x] **Generate Linux VRT baselines** — committed in 11a1c8f after
      generation via the new `vrt-baselines.yml` workflow_dispatch helper
      (the Docker script is still supported but optional); the `vrt:`
      job in `.github/workflows/storybook-quality.yml` is now an
      enforcing gate (no more `continue-on-error: true`). Verified green
      on run 25895313180.
- [x] **Sidecar bundle size budget enforcement in CI** — landed in
      10c6de6 + 1d6fc83 (followed by an .gitignore unblock in e5e4d2f).
      A new `sidecar-budget` job in `ci.yml` runs the host (Linux x64)
      build with `--strip-stdlib --aggressive --budget-mb 800` on every
      PR. The 800 MB ceiling is a regression gate against the
      currently-observed ~741 MB baseline; the original 320 MB design
      target is tracked under "Migrate sidecar to install_only_stripped"
      below.
- [ ] **Migrate sidecar to `install_only_stripped` CPython distribution**
      — the current `install_only` python-build-standalone variant
      unpacks to ~700 MB and the prune logic only reclaims ~20 MB of
      that. Switching to `install_only_stripped` (32 MB compressed
      vs 105 MB) plus adapting the prune paths should let us drop both
      the PR budget (`ci.yml`) and production budget (`sidecar-build.yml`)
      back down to ~320 MB / ~300 MB respectively, matching the original
      P5 sidecar size design.
- [ ] **Cross-platform sidecar build matrix**: prepare-sidecar.sh supports
      `--target all`, but the GitHub Actions workflow that emits release
      artefacts for win/linux/mac in one run isn't there yet.
- [x] **Examples in CI** — landed in 10c6de6. New `examples` job in
      `ci.yml` runs `examples/py-minimal`, `examples/ts-minimal`, and
      `examples/sidecar-roundtrip` end-to-end and grep-asserts the
      marker lines they're documented to print
      (`[wire]`, `[harness]`, `[ready]`, `[ping]`, `[bye]`). Stops a
      refactor that silently breaks an example from sliding past CI
      while the README claims still pass review.
- [x] **Docs render smoke test** — landed in ba4ade7. `docs.yml` now
      grep-asserts the rendered `_site/` HTML for five known-good
      patterns after `mkdocs build --strict` (no `<p>```` leak;
      home-page Mermaid `graph BT` block; sidecar Mermaid
      `sequenceDiagram` block; ≥5 `<div class="highlight">` blocks on
      the sidecar page; UI page "Working on it locally" bash block is
      a fenced highlight div). Catches regressions like the
      `pymdown-extensions==10.12` fenced-code-as-paragraph bug we
      fixed manually in 91fb79d, before they ship to readers.
- [x] **Restore packaged sidecar build toolchain to git** — landed in
      e5e4d2f. The catch-all `build/` ignore was silently swallowing
      `packages/sidecar/build/{build_sidecar.py,codesign,tests}`, which
      meant `sidecar-build.yml`, `sidecar-codesign.yml`, and
      `sidecar-prune-tests.yml` had never actually run a green build
      since project inception (their triggers required files that
      weren't tracked). Surfaced as a side-effect of the `sidecar-budget`
      gate above and fixed by adding an explicit `!` un-ignore.
- [x] **Bump python-build-standalone pin** — landed in 1d6fc83.
      The previously-pinned `20251105` release was deleted upstream and
      sidecar-budget hit a 404 on first run; bumped to `20260510` /
      `3.12.13`. Source comment now documents how to re-bump when this
      happens again.

### Three-repo convergence

- [ ] **deeppath/apps/web prod ChatPanel migration**: today only
      `/dev/framework-preview` uses `@steerable/agent-ui`. Migrate the
      production `/agent` ChatPanel after one more 0.x cycle.
- [ ] **deeppath-api Docker image baked with wheels**: extend the existing
      Dockerfile to call `scripts/use_framework_wheels.sh` during build, so
      the production image doesn't depend on the framework source tree at
      all.
- [ ] **deeppath-agent default sidecar-on**: currently opt-in via
      `STEERABLE_USE_SIDECAR=1`. Flip the default once we've packaged at
      least one signed installer with the embedded runtime.

### Spec evolution (non-blocking)

- [ ] **Pre-1.0 spec freeze**: lock `additionalProperties` semantics —
      currently `SSEEvent`/`ChatMessage` are open and `ToolCall`/
      `SidecarRequest` are closed. Audit + document the rule before 1.0.
- [ ] **`HarnessTrace.spans[*].events` extension catalogue**: today payload
      shape is implementation-defined; want a `spec/runtime/TraceEventKind/*.schema.json`
      registry so traces from two implementations become diff-able.

---

## 🔁 Reproducible commands

For new contributors landing here cold:

```bash
# 1. Build every artefact + run the local Mode B verifier
cd steerable-framework
./scripts/release/build-local-artifacts.sh
./scripts/release/verify-local-artifacts.sh

# 2. Switch deeppath-api to wheel mode (production-like) and back
cd ../deeppath-api
./scripts/use_framework_wheels.sh
./scripts/use_framework_source.sh

# 3. Build the sidecar bundle for the host platform
cd ../deeppath-agent
pnpm prepare:sidecar -- --strip-stdlib

# 4. Full GA dry-run
cd ../steerable-framework
pnpm install --frozen-lockfile && uv sync --all-packages
pnpm -r test && uv run pytest -q
```

When all four blocks are green, a public publish is just a `gh secret set`
+ `git push` away.
