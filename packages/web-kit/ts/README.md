# @steerable/web-kit

`@steerable/web-kit` is the frontend UI base for the Steerable Framework (Tier 4). It provides robust theme providers, remote/local runtime adapters, protocol-driven action parsers, and side-by-side workspace panel slots.

## Features

- **Theme & Brand Provider (`SteerableConfigProvider`)**: Easily style all Steerable widgets with uniform colors and radius settings while keeping standard branding completely customizable.
- **Runtime Adapter (`SteerableRuntimeProvider`)**: Seamlessly switch your React app between **remote** (cloud-backed FastAPI SSE endpoints) and **local** (Electron IPC sidecar backend) modes without changing component code.
- **Action & Tool Renderer Utility**: Parses message contents for action tags (like `<dp-action .../>`) and summarizes complex tool result shapes into human-readable labels.
- **Panel Slot System (`WorkspaceShell` / `PanelProvider`)**: Standard Notion-style三栏 workspace layout displaying a list of customizable business views alongside an optional AI Assistant chat pane.
- **High-level App Assembly (`SteerableWebApp`)**: A modular, ready-to-use out-of-the-box shell that bundles all providers, panels, and chat systems.

## Installation

```bash
pnpm add @steerable/web-kit
```

### Peer Dependencies
- `react` (e.g. `>=18.0.0`)
- `react-dom` (e.g. `>=18.0.0`)

## Usage

### 1. Wrapping with standard Providers
```tsx
import { SteerableWebApp } from '@steerable/web-kit';

const theme = {
  colors: {
    background: '#ffffff',
    foreground: '#09090b',
    card: '#ffffff',
    primary: '#18181b',
    muted: '#f4f4f5',
    accent: '#f4f4f5',
    border: '#e4e4e7'
  },
  radius: '0.5rem'
};

const branding = {
  productName: '时踪',
  tagline: 'AI 行动助手',
  logo: <MyLogoIcon />,
  domain: 'deeppath.cc'
};

const runtime = {
  mode: 'remote' as const,
  apiBaseUrl: 'http://localhost:8000',
  getAuthToken: () => localStorage.getItem('token')
};

const panels = [
  {
    id: 'tasks',
    label: '任务管理',
    icon: <TaskIcon />,
    component: () => <TaskPanel />
  }
];

export default function App() {
  return (
    <SteerableWebApp
      theme={theme}
      branding={branding}
      runtime={runtime}
      workspacePanels={panels}
      chat={<MyChatPanel />}
    />
  );
}
```

### 2. Consuming Runtime and APIs
```tsx
import { useSteerableRuntime } from '@steerable/web-kit';

function ChatBox() {
  const runtime = useSteerableRuntime();

  const handleSend = async () => {
    // Standard request (remote/local agnostic)
    const result = await runtime.request('POST', '/api/v2/chats', { query: 'Hello' });

    // Stream SSE packets
    const stream = runtime.stream('POST', '/api/v2/chats/stream', { query: 'Hello' });
    for await (const chunk of stream) {
      console.log('Received frame:', chunk);
    }
  };

  return <button onClick={handleSend}>Send Message</button>;
}
```

### 3. Parsing XML Action Tags in Message Body
```typescript
import { processActionTags } from '@steerable/web-kit';

const rawMessage = "I've created your task. <dp-action type=\"create_task\" params=\"{'title':'Study English'}\" />";
const processed = processActionTags(rawMessage);

console.log(processed.hasActions); // true
console.log(processed.content);    // "I've created your task. <!-- SLOT:action_1 -->"
console.log(processed.actions[0]); // { id: 'action_1', type: 'create_task', params: { title: 'Study English' } }
```

## License
Apache-2.0
