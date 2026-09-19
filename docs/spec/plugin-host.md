# Plugin host protocol

Python plugins depend on `steerable-plugin-sdk`, not
`steerable-agent-runtime`. A plugin host owns discovery and handler execution;
the sidecar projects its tool descriptors into the loop through RPC.

The protocol is newline-delimited JSON-RPC 2.0 over a private process channel.
Version `0.1.0` defines these sidecar-to-host methods:

| Method | Params | Result |
| --- | --- | --- |
| `plugin.host.ping` | `{protocolVersion}` | `{status, protocolVersion, toolsCount}` |
| `plugin.tools.describe` | `{}` | `{tools: ToolDescriptor[]}` |
| `plugin.tool.invoke` | `{name, arguments, context}` | `ToolResult` |
| `plugin.list` | `{}` | `{plugins: PluginRecord[]}` |
| `plugin.enable` | `{name}` | `{plugin: PluginRecord}` |
| `plugin.disable` | `{name}` | `{plugin: PluginRecord}` |
| `plugin.reload` | `{name}` | `{plugin: PluginRecord}` |

`ToolDescriptor` contains `name`, `description`, `schema`, `mode`, `exposure`,
`requireConsent`, `concurrencySafe`, and nullable `plugin`. `ToolResult`
matches `steerable-agent-protocol`.

Protocol mismatch, malformed descriptors, unknown lifecycle targets, and
handler failures are errors. A sidecar must not advertise a plugin tool until
the host has completed `plugin.host.ping` and `plugin.tools.describe`.
