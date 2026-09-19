use async_trait::async_trait;

use crate::errors::LLMError;
use crate::types::{LLMMessage, ToolCall, ToolResult};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CompletionDraft {
    pub status: String,
    pub reason: String,
    pub content: String,
    pub round_index: u32,
    pub had_tool_calls: bool,
    pub tool_calls_used: u32,
    pub tool_successes: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HookAction {
    Accept,
    Retry { message: String, reason: String },
    Narrate { message: String, reason: String },
}

#[derive(Clone, Debug, PartialEq)]
pub enum PreStepOutcome {
    Proceed {
        appends: Vec<LLMMessage>,
        rewrite: Option<Vec<LLMMessage>>,
        reason: Option<String>,
        action: String,
        pre_tokens: Option<i64>,
        post_tokens: Option<i64>,
        tool_choice: Option<String>,
    },
    Reject {
        reason: String,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub enum RequestErrorAction {
    Fail {
        reason: String,
    },
    Retry {
        delay_ms: u64,
        reason: String,
        rewrite: Option<Vec<LLMMessage>>,
    },
}

impl Default for PreStepOutcome {
    fn default() -> Self {
        Self::Proceed {
            appends: Vec::new(),
            rewrite: None,
            reason: None,
            action: "append".into(),
            pre_tokens: None,
            post_tokens: None,
            tool_choice: None,
        }
    }
}

#[async_trait]
pub trait LoopHooks: Send + Sync {
    async fn before_completion(&self, _draft: &CompletionDraft) -> HookAction {
        HookAction::Accept
    }

    async fn pre_step(&self, _transcript: &[LLMMessage]) -> PreStepOutcome {
        PreStepOutcome::default()
    }

    async fn post_tool_result(&self, result: ToolResult, _call: &ToolCall) -> ToolResult {
        result
    }

    async fn on_request_error(
        &self,
        error: &LLMError,
        _transcript: &[LLMMessage],
        _round_index: u32,
    ) -> RequestErrorAction {
        RequestErrorAction::Fail {
            reason: error.to_string(),
        }
    }

    fn wrap_up_may_drop_tools(&self) -> bool {
        true
    }
}

#[derive(Clone, Debug, Default)]
pub struct NoopHooks;

#[async_trait]
impl LoopHooks for NoopHooks {}
