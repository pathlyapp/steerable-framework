# DeepPath Migration Notes

This repo is extracted from DeepPath and keeps the same contract philosophy:
**define protocol once, generate for TS/PY, verify parity**.

## Package mapping

- DeepPath shared protocol models -> `@steerable/agent-protocol` / `steerable-agent-protocol`
- DeepPath agent runtime helpers -> `@steerable/agent-harness` / `steerable-agent-harness`

## Migration checklist

1. Replace app-local protocol types with package imports from Steerable.
2. Replace duplicated retry/budget/policy helpers with harness helpers.
3. Keep tool result semantics aligned with `ToolResult` (`terminal`, `needsFollowup`).
4. Move stream envelopes to `SSEEvent` type names and event enums.

## Known naming differences (TS vs PY)

- TypeScript uses camelCase fields (for example `maxToolCalls`, `needsFollowup`)
- Python helpers expose snake_case function names but preserve protocol field names
  where required by schema interoperability

## Validation flow

After migration:

```bash
pnpm gen
pnpm test
uv run pytest tests/conformance/py
```
