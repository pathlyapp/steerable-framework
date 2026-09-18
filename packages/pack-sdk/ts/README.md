# @steerable/pack-sdk

Scenario pack contract for `@steerable/agent-shell` — pure types, zero runtime.

A scenario pack declares every extension a vertical scenario needs (services,
tools, tables, skills, agent seeds, IPC, HTTP routes, preload bridge, renderer
slots, branding), and the host composes the selected packs at build time. This
package ships only `types/`; there is no runtime code.

License: Apache-2.0.
