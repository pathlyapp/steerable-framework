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

Products that require authentication can register an `AppShellGate` from
`@steerable/agent-shell-web/auth/gate` before calling `bootstrap()`. An
enabled gate renders before the application router and calls
`onAuthenticated` to continue normal bootstrap. With no gate, or a disabled
gate, startup is unchanged.

License: Apache-2.0.
