# Steerable Plugin SDK

Pure-Python plugin authoring surface for Steerable tools. This package does
not depend on `steerable-agent-runtime` or its native CoreLoop wheel.

```python
from steerable_plugin_sdk import tool


def register(router):
    @tool(router=router, description="Greet by name")
    async def greet(name: str) -> str:
        return f"hello {name}"
```

Packaged plugins publish `register` through the `steerable.tools` entry-point
group. `LocalPluginRouter` is the plugin-host registry; `PluginHostRpcClient`
and `RpcRouterProxy` bridge its descriptors and invocations to a sidecar
router.
