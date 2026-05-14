export interface ChatAgent {
  id: string;
  slug?: string;
  name: string;
  icon?: string;
  color?: string;
  description?: string;
  rolePrompt?: string;
  forbiddenPrompt?: string;
  skillIds?: string[];
  allowExternalSkills?: boolean;
  isBuiltin?: boolean;
  isArchived?: boolean;
  sortOrder?: number;
  createdAt: string;
  updatedAt: string;
  [k: string]: unknown;
}
