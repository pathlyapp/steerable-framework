# TypeScript Minimal Example

Smoke test that exercises the TypeScript Tier 1 (`agent-protocol`) and Tier 2
(`agent-harness`) facades together.

## Run

From the framework monorepo root:

```bash
pnpm install
pnpm --filter steerable-example-ts-minimal start
```

Expected output:

```
[harness] mode=read  exhausted=false  state={"tokensUsed":120,"stepsUsed":1,"toolCallsUsed":1}
[wire]    {"type":"tool_result","payload":{"callId":"call_1","result":{...}}}
```

## Notes

- Production browser/Electron code should reach for [`@steerable/agent-ui`](../../packages/agent-ui/ts) instead of importing the harness directly — the harness facade exists mainly for cross-language conformance tests.
- For the full UI surface, see the dev-only `/dev/framework-preview` page in `deeppath/apps/web`.
