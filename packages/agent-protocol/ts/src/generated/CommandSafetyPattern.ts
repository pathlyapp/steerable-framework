export interface CommandSafetyPattern {
  id: string;
  label: string;
  description: string;
  pattern: string;
  category: string;
  severity: "critical" | "warning";
  platform: "all" | "unix" | "windows";
}
