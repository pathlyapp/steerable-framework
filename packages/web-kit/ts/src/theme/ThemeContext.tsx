import React, { createContext, useContext } from 'react';

export interface SteerableTheme {
  colors: {
    background: string;
    foreground: string;
    card: string;
    primary: string;
    muted: string;
    accent: string;
    border: string;
  };
  radius: string;
}

export interface Branding {
  productName: string;
  tagline: string;
  logo: React.ReactNode;
  domain: string;
}

export interface SteerableConfig {
  theme?: SteerableTheme;
  branding?: Branding;
}

const SteerableConfigContext = createContext<SteerableConfig>({});

export const SteerableConfigProvider: React.FC<{
  config: SteerableConfig;
  children: React.ReactNode;
}> = ({ config, children }) => {
  return (
    <SteerableConfigContext.Provider value={config}>
      {children}
    </SteerableConfigContext.Provider>
  );
};

export function useSteerableConfig(): SteerableConfig {
  return useContext(SteerableConfigContext);
}
