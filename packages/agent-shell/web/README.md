# @steerable/agent-shell-web

Product-neutral renderer SPA source for `@steerable/agent-shell` (React + Vite).

This is a **source package**, not a build artifact. Product web entry points
consume it via the `@/` alias (anchored at this package's `src/`) and the
`createProductViteConfig` factory exported from `./vite.base`:

```ts
import { createProductViteConfig } from '@steerable/agent-shell-web/vite.base';

export default createProductViteConfig({ productDir, flavor });
```

The product's Vite build compiles this package's `src/` into the product's own
bundle; there is no prebuilt `dist/` here. See
[`docs/spec/architecture.md`](https://github.com/pathlyapp/steerable-framework/blob/develop/docs/spec/architecture.md)
for the tier model.

License: Apache-2.0.
