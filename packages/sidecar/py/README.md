# steerable-sidecar

The portable Python entrypoint for the **steerable** framework. Designed to be
spawned by an Electron / desktop host (or directly from a CLI) and addressed
via stdio JSON-RPC 2.0.

## Quick start

```bash
pip install steerable-sidecar
python -m steerable_sidecar
```

Or use the console script:

```bash
steerable-sidecar
```

The process:

1. Initializes the in-memory storage and a default tool router.
2. Prints the **ready signal** on **stderr** as a single line:
   `__SIDECAR_READY__:<json>` matching `spec/sidecar/SidecarHealth`.
3. Begins reading JSON-RPC frames from stdin (one frame per line).
4. Writes responses and notifications to stdout.

See `spec/sidecar/README.md` for the full method/notification catalog.

## Embedding

```python
import asyncio
from steerable_sidecar import Sidecar

async def main() -> None:
    sidecar = Sidecar()
    sidecar.tools.register(my_tool)
    await sidecar.serve()

asyncio.run(main())
```
