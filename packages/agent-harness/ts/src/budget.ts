export interface BudgetLimit {
  maxTokens: number;
  maxSteps: number;
  maxToolCalls: number;
}

export interface BudgetState {
  tokensUsed: number;
  stepsUsed: number;
  toolCallsUsed: number;
}

export interface BudgetConsumeOptions {
  tokens?: number;
  step?: boolean;
  toolCall?: boolean;
}

export function consumeBudget(
  state: BudgetState,
  limits: BudgetLimit,
  options: BudgetConsumeOptions = {}
): { state: BudgetState; exhausted: boolean } {
  const nextState: BudgetState = {
    tokensUsed: state.tokensUsed + Math.max(options.tokens ?? 0, 0),
    stepsUsed: state.stepsUsed + (options.step ? 1 : 0),
    toolCallsUsed: state.toolCallsUsed + (options.toolCall ? 1 : 0),
  };
  const exhausted =
    nextState.tokensUsed > limits.maxTokens ||
    nextState.stepsUsed > limits.maxSteps ||
    nextState.toolCallsUsed > limits.maxToolCalls;
  return { state: nextState, exhausted };
}
