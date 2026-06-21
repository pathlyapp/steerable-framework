# @steerable/desktop-kit

`@steerable/desktop-kit` is the client-side desktop base for the Steerable Framework (Tier 3). It provides robust local capabilities for building Electron-based AI agent desktop applications.

## Features

- **Sidecar Supervision (`SidecarSupervisor`)**: Easily spawn, monitor, and interact with the portable Python `steerable-sidecar` subprocess. Features automatic health checks, JSON-RPC communication, auto-restart, and safe termination upon application exit.
- **PTY Terminal Management (`TerminalManager`)**: Run visible, interactive terminal sessions (`node-pty`) driven by both the user and the AI agent, capturing commands with high precision via invisible escape code sentinels.
- **Local Executor (`LocalExecutor`)**: Securely execute shell commands, read/write local files, and open system paths with built-in protection against dangerous command execution.
- **Local Tool Router (`LocalToolRouter`)**: Map core local executor functions (`local_exec_shell`, `local_read_file`, `local_write_file`, `local_open_path`) into the AI agent's Tool registration specs.
- **IPC Bridge Contract (`ElectronBridge`)**: Clear, strongly typed definitions for IPC communication between main and renderer processes.

## Installation

```bash
pnpm add @steerable/desktop-kit
```

### Peer Dependencies
Since this package integrates tightly with Electron and compiles native C++ modules, ensure you have these peer dependencies installed in your application:

- `electron` (e.g., `^28.0.0` or higher)
- `node-pty` (e.g., `^1.1.0`)

## Usage

### 1. Spawning the Python Sidecar
```typescript
import { SidecarSupervisor } from '@steerable/desktop-kit';

// Start the supervisor. Will auto-locate the portable python bundle or use overrides.
const supervisor = await SidecarSupervisor.start({
  healthIntervalMs: 5000,
});

// Ping to check health
const health = await supervisor.ping();
console.log('Sidecar is up! Protocol:', health.protocolVersion);

// Graceful shutdown on app quit
await supervisor.shutdown();
```

### 2. Managing Terminal Sessions
```typescript
import { TerminalManager } from '@steerable/desktop-kit';

const manager = new TerminalManager();

// Spawn a new interactive bash terminal
const session = manager.spawn({ command: '/bin/bash' });

// Listen to raw PTY data chunk output
manager.on('data', (sessionId, chunk) => {
  if (sessionId === session.id) {
    process.stdout.write(chunk);
  }
});

// Send input to the terminal
manager.write(session.id, 'ls -la\n');
```

## Packaging the Portable Python Runtime

This kit includes helpers to package an embedded portable Python environment containing all framework dependencies.

To build the wheels and copy the runtime into your Electron `resources/`:

```bash
# 1. Compile the Python framework wheels
./node_modules/@steerable/desktop-kit/scripts/prepare-framework-wheels.sh

# 2. Package the portable sidecar runtime
./node_modules/@steerable/desktop-kit/scripts/prepare-sidecar.sh
```

Then configure `electron-builder` in `package.json` to include the extra resource:

```json
"extraResources": [
  {
    "from": "resources/python-runtime",
    "to": "python-runtime",
    "filter": [
      "**/*",
      "!_cache${/*}"
    ]
  }
]
```

## License
Apache-2.0
