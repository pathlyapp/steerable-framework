import React from 'react';
import { usePanels, PanelProvider, PanelDef } from './PanelContext.js';

export interface WorkspaceShellProps {
  chat?: React.ReactNode;
  panels: PanelDef[];
  defaultActiveId?: string;
}

const WorkspaceShellInner: React.FC<{ chat?: React.ReactNode }> = ({ chat }) => {
  const { panels, activePanelId, setActivePanelId } = usePanels();

  const activePanel = panels.find((p) => p.id === activePanelId);

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background text-foreground">
      {/* 1. Left Sidebar: Panel Switcher */}
      <div className="flex w-64 flex-col border-r border-border bg-card">
        <div className="flex h-14 items-center px-4 border-b border-border">
          <span className="font-semibold text-foreground tracking-wide">Workspace</span>
        </div>
        <div className="flex-1 overflow-y-auto p-3 space-y-1">
          {panels.map((panel) => {
            const isActive = panel.id === activePanelId;
            return (
              <button
                key={panel.id}
                onClick={() => setActivePanelId(panel.id)}
                className={`flex w-full items-center gap-3 px-3 py-2 text-sm font-medium rounded-md transition-all duration-200 ${
                  isActive
                    ? 'bg-accent text-foreground shadow-sm'
                    : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                }`}
              >
                <div className="flex h-5 w-5 items-center justify-center">
                  {panel.icon}
                </div>
                <span>{panel.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* 2. Middle Panel Area: Selected workspace view */}
      <div className="flex-1 flex flex-col min-w-0 bg-background">
        <div className="flex h-14 items-center justify-between px-6 border-b border-border bg-card">
          <h2 className="text-base font-semibold text-foreground">
            {activePanel ? activePanel.label : 'Select a panel'}
          </h2>
        </div>
        <div className="flex-1 overflow-auto p-6">
          {panels.map((panel) => {
            const ActiveComponent = panel.component;
            const isActive = panel.id === activePanelId;
            return (
              <div
                key={panel.id}
                className={`h-full w-full ${isActive ? 'block' : 'hidden'}`}
              >
                <ActiveComponent active={isActive} />
              </div>
            );
          })}
        </div>
      </div>

      {/* 3. Right Sidebar: Chat Assistant Pane (optional) */}
      {chat && (
        <div className="w-[450px] border-l border-border bg-card flex flex-col h-full">
          <div className="flex h-14 items-center px-4 border-b border-border">
            <span className="font-semibold text-foreground">AI Assistant</span>
          </div>
          <div className="flex-1 overflow-hidden">{chat}</div>
        </div>
      )}
    </div>
  );
};

export const WorkspaceShell: React.FC<WorkspaceShellProps> = ({
  chat,
  panels,
  defaultActiveId,
}) => {
  return (
    <PanelProvider panels={panels} defaultActiveId={defaultActiveId}>
      <WorkspaceShellInner chat={chat} />
    </PanelProvider>
  );
};
