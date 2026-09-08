/** Default for {@link BudgetLimit.cachedTokenWeight}. */
export const DEFAULT_CACHED_TOKEN_WEIGHT = 0.1;

export interface BudgetLimit {
  maxTokens: number;
  maxSteps: number;
  maxToolCalls: number;
  /**
   * Fraction of a normal token charged for prompt tokens the provider served
   * from its cache. The token budget is a cost proxy, and a cache hit is
   * priced well below a miss (~0.1x for Anthropic cache-read and DeepSeek
   * cache-hit, ~0.5x for OpenAI cached input). An agentic turn re-sends a
   * mostly identical prefix every round, so charging hits at par exhausts
   * the budget several times sooner than the run's cost warrants. Hosts on
   * providers with pricier caches raise it. Inert unless the caller reports
   * `cachedTokens`; omitted → {@link DEFAULT_CACHED_TOKEN_WEIGHT}.
   */
  cachedTokenWeight?: number;
}

export interface BudgetState {
  tokensUsed: number;
  stepsUsed: number;
  toolCallsUsed: number;
}

export interface BudgetConsumeOptions {
  /** The request's total usage. */
  tokens?: number;
  /** The subset of `tokens` served from the provider's prompt cache. */
  cachedTokens?: number;
  step?: boolean;
  toolCall?: boolean;
}

/**
 * Charge one request against the budget, returning the new state.
 *
 * The cache discount is floored to an integer before subtraction so this
 * stays bit-identical to the Python port (conformance case
 * `cases/budget/cached.yaml`); a provider that reports more cached tokens
 * than total clamps to a zero charge rather than refunding budget.
 */
export function consumeBudget(
  state: BudgetState,
  limits: BudgetLimit,
  options: BudgetConsumeOptions = {}
): { state: BudgetState; exhausted: boolean } {
  let billedTokens = Math.max(options.tokens ?? 0, 0);
  const cached = Math.max(options.cachedTokens ?? 0, 0);
  if (cached) {
    const weight = limits.cachedTokenWeight ?? DEFAULT_CACHED_TOKEN_WEIGHT;
    billedTokens = Math.max(
      billedTokens - cached + Math.floor(cached * weight),
      0
    );
  }
  const nextState: BudgetState = {
    tokensUsed: state.tokensUsed + billedTokens,
    stepsUsed: state.stepsUsed + (options.step ? 1 : 0),
    toolCallsUsed: state.toolCallsUsed + (options.toolCall ? 1 : 0),
  };
  const exhausted =
    nextState.tokensUsed > limits.maxTokens ||
    nextState.stepsUsed > limits.maxSteps ||
    nextState.toolCallsUsed > limits.maxToolCalls;
  return { state: nextState, exhausted };
}
