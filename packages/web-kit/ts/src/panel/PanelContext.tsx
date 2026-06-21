import React, { createContext, useContext, useState } from 'react';

export interface PanelProps {
  active: boolean;
}

export interface PanelDef {
  id: string;
  label: string;
  icon: React.ReactNode;
  component: React.ComponentType<PanelProps>;
  visible?: () => boolean;
}

export interface PanelState {
  panels: PanelDef[];
  activePanelId: string | null;
  setActivePanelId: (id: string | null) => void;
}

const PanelContext = createContext<PanelState | null>(null);

export const PanelProvider: React.FC<{
  panels: PanelDef[];
  defaultActiveId?: string;
  children: React.ReactNode;
}> = ({ panels, defaultActiveId, children }) => {
  const [activePanelId, setActivePanelId] = useState<string | null>(
    defaultActiveId || (panels.length > 0 ? panels[0].id : null)
  );

  const visiblePanels = panels.filter((p) => !p.visible || p.visible());

  return (
    <PanelContext.Provider
      value={{
        panels: visiblePanels,
        activePanelId,
        setActivePanelId,
      }}
    >
      {children}
    </PanelContext.Provider>
  );
};

export function usePanels(): PanelState {
  const context = useContext(PanelContext);
  if (!context) {
    throw new Error('usePanels must be used within a PanelProvider');
  }
  return context;
}
