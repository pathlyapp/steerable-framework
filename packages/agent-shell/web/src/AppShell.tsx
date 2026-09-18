import { Outlet } from 'react-router-dom';
import { isElectron } from './lib/electron-bridge';
import { isDemoMode } from './lib/demo-flag';

export function AppShell() {
  return (
    <div className="flex h-full w-full flex-col bg-agent-canvas text-agent-foreground">
      <main className="flex flex-1 overflow-hidden">
        <Outlet />
      </main>
      {isDemoMode() ? (
        <div className="border-t border-agent-border bg-agent-muted px-3 py-1 text-xs text-agent-muted-foreground">
          Demo &middot; simulated data, no live model
        </div>
      ) : (
        !isElectron() && (
          <div className="border-t border-agent-border bg-agent-muted px-3 py-1 text-xs text-agent-muted-foreground">
            Browser preview mode &middot; running outside Electron, IPC bridge unavailable
          </div>
        )
      )}
    </div>
  );
}
