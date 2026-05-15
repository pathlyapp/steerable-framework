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
- [x] **p1-publish-protocol** `@steerable/agent-protocol@0.1.0` (npm) +
      `steerable-agent-protocol==0.1.0` (PyPI) artefacts built; publishing
      blocked on credentials (see below).
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

- [ ] **Reserve npm scope `@steerable`** (`npm org create steerable`).
- [ ] **Reserve PyPI projects** `steerable-agent-protocol`,
      `steerable-agent-harness`, `steerable-agent-runtime`,
      `steerable-sidecar` (or wire Trusted Publisher).
- [ ] **GitHub Actions secrets** `NPM_TOKEN`, `PYPI_API_TOKEN`.
- [x] **Push framework repo to GitHub remote** — pushed to
      `git@github.com:pathlyapp/steerable-framework.git`; release-please
      will open its first release PR once `NPM_TOKEN`/`PYPI_API_TOKEN` land.
- [ ] **Cut & publish `0.1.0` on the public registries** (or `0.2.0` if any
      breaking change shipped between now and the push).

### Public docs site

- [x] **GitHub Pages enabled** (workflow source) → docs site live at
      <https://pathlyapp.github.io/steerable-framework/>.
- [x] **Storybook embedded under `/storybook/`** → live at
      <https://pathlyapp.github.io/steerable-framework/storybook/>.
- [ ] **Bump GitHub Actions to Node.js 24-compatible versions** before
      2026-09-16 (`actions/checkout`, `actions/setup-node`,
      `actions/setup-python`, `actions/upload-artifact`,
      `pnpm/action-setup` all surface a Node 20 deprecation warning).

### Toward a real 1.0.0

- [ ] **One stable minor cycle on 0.x** before promoting to 1.0.0 — current
      API is only just being exercised by the three repos.
- [ ] **Decide: shared `1.0.0` for protocol + harness?** Lock-step is enforced,
      but Tier 2/3/4 packages can independently version once 1.0 lands.

### Quality / DevX gaps

- [ ] **Codegen idempotency**: `pnpm gen` produces stable diff across runs
      now, but we don't yet enforce in CI that running it twice in a row
      yields zero diff (catch sneaky non-determinism early).
- [x] **Generate Linux VRT baselines** — committed in 11a1c8f after
      generation via the new `vrt-baselines.yml` workflow_dispatch helper
      (the Docker script is still supported but optional); the `vrt:`
      job in `.github/workflows/storybook-quality.yml` is now an
      enforcing gate (no more `continue-on-error: true`). Verified green
      on run 25895313180.
- [ ] **Sidecar bundle size budget enforcement in CI**: `--budget-mb` is
      implemented; CI doesn't run `--target all --strip-stdlib --budget-mb 320`
      yet.
- [ ] **Cross-platform sidecar build matrix**: prepare-sidecar.sh supports
      `--target all`, but the GitHub Actions workflow that emits release
      artefacts for win/linux/mac in one run isn't there yet.
- [ ] **Examples in CI**: `examples/{py-minimal,ts-minimal,sidecar-roundtrip}`
      run locally via `uv run pytest` / `node`; add a CI job that runs each
      example end-to-end so README claims stay honest.

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
