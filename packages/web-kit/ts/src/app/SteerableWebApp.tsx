import React from 'react';
import { SteerableConfigProvider, SteerableTheme, Branding } from '../theme/ThemeContext.js';
import { SteerableRuntimeProvider, RuntimeConfig } from '../runtime/RuntimeContext.js';
import { WorkspaceShell } from '../panel/WorkspaceShell.js';
import { PanelDef } from '../panel/PanelContext.js';

export interface SteerableWebAppProps {
  theme?: SteerableTheme;
  branding?: Branding;
  runtime: RuntimeConfig;
  workspacePanels?: PanelDef[];
  chat?: React.ReactNode;
  defaultActivePanelId?: string;
}

export const SteerableWebApp: React.FC<SteerableWebAppProps> = ({
  theme,
  branding,
  runtime,
  workspacePanels = [],
  chat,
  defaultActivePanelId,
}) => {
  return (
    <SteerableConfigProvider config={{ theme, branding }}>
      <SteerableRuntimeProvider config={runtime}>
        <WorkspaceShell
          panels={workspacePanels}
          chat={chat}
          defaultActiveId={defaultActivePanelId}
        />
      </SteerableRuntimeProvider>
    </SteerableConfigProvider>
  );
};
