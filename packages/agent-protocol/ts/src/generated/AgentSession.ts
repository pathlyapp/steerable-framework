export interface AgentSession {
  id?: string;
  sessionId: string;
  userId: string;
  projectId?: string | null;
  chatId: string;
  currentStage: string;
  nextStage?: string | null;
  scenario?: string;
  stageData?: Record<string, unknown> | null;
  isActive: boolean;
  createdAt: string;
  updatedAt: string;
  [k: string]: unknown;
}
