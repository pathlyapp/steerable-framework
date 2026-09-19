//! Token/step/tool-call budget. Bit-identical to `steerable_agent_harness.budget`.

pub const DEFAULT_CACHED_TOKEN_WEIGHT: f64 = 0.1;

#[derive(Clone, Debug, PartialEq)]
pub struct BudgetLimit {
    pub max_tokens: i64,
    pub max_steps: i64,
    pub max_tool_calls: i64,
    pub cached_token_weight: f64,
}

impl BudgetLimit {
    pub fn new(max_tokens: i64, max_steps: i64, max_tool_calls: i64) -> Self {
        Self {
            max_tokens,
            max_steps,
            max_tool_calls,
            cached_token_weight: DEFAULT_CACHED_TOKEN_WEIGHT,
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct BudgetState {
    pub tokens_used: i64,
    pub steps_used: i64,
    pub tool_calls_used: i64,
}

/// Charge one request. Cached tokens are discounted then floored, matching
/// the TypeScript `cases/budget/cached.yaml` conformance case.
pub fn consume_budget(
    state: &BudgetState,
    limits: &BudgetLimit,
    tokens: i64,
    cached_tokens: i64,
    step: bool,
    tool_call: bool,
) -> (BudgetState, bool) {
    let mut billed = tokens.max(0);
    let cached = cached_tokens.max(0);
    if cached > 0 {
        billed =
            (billed - cached + (cached as f64 * limits.cached_token_weight).floor() as i64).max(0);
    }
    let next = BudgetState {
        tokens_used: state.tokens_used + billed,
        steps_used: state.steps_used + i64::from(step),
        tool_calls_used: state.tool_calls_used + i64::from(tool_call),
    };
    let exhausted = next.tokens_used > limits.max_tokens
        || next.steps_used > limits.max_steps
        || next.tool_calls_used > limits.max_tool_calls;
    (next, exhausted)
}
