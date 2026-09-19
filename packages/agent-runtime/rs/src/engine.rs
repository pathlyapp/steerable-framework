use futures_util::future::join_all;
use serde_json::{json, Value};
use std::collections::HashSet;

use crate::budget::{consume_budget, BudgetLimit, BudgetState};
use crate::executor::ToolExecutor;
use crate::hooks::{
    CompletionDraft, HookAction, LoopHooks, NoopHooks, PreStepOutcome, RequestErrorAction,
};
use crate::provider::{LLMProvider, LLMUsage};
use crate::types::{
    ContentPart, LLMMessage, LoopContext, LoopEvent, ToolCall, ToolResult, MAX_COMPLETION_REDOS,
    MAX_READ_IMAGES_PER_ROUND,
};

#[derive(Clone, Debug)]
pub struct LoopConfig {
    pub max_rounds: u32,
    pub max_tool_errors: u32,
    pub budget: Option<BudgetLimit>,
    pub persist_tool_results: bool,
    pub parallel_tools: bool,
    pub tool_dedup: bool,
    pub tool_timeout_ms: Option<u64>,
    pub soft_timeout_ms: Option<u64>,
    pub wrap_up_keeps_tools: bool,
    pub wrap_up_max_tool_rounds: u32,
    pub wrap_up_tool_timeout_ms: Option<u64>,
    pub wrap_up_hard_cap_ms: Option<u64>,
    pub steer_mode: String,
}

impl Default for LoopConfig {
    fn default() -> Self {
        Self {
            max_rounds: 32,
            max_tool_errors: 3,
            budget: None,
            persist_tool_results: false,
            parallel_tools: true,
            tool_dedup: true,
            tool_timeout_ms: Some(300_000),
            soft_timeout_ms: None,
            wrap_up_keeps_tools: false,
            wrap_up_max_tool_rounds: 4,
            wrap_up_tool_timeout_ms: None,
            wrap_up_hard_cap_ms: None,
            steer_mode: "boundary".into(),
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct RoundControl {
    pub cancel: bool,
    pub interrupt: bool,
    pub steers: Vec<String>,
}

pub struct CoreLoop<P, E, H = NoopHooks> {
    provider: P,
    executor: E,
    hooks: H,
    config: LoopConfig,
    tools: Vec<Value>,
    poll_control: Option<Box<dyn FnMut(bool) -> RoundControl + Send>>,
    history_observer: Option<Box<dyn FnMut(&[LLMMessage]) + Send>>,
    context_observer: Option<Box<dyn FnMut(&LoopContext, u32) + Send>>,
    pub last_run_usage: Option<LLMUsage>,
    pub last_history: Vec<LLMMessage>,
}

impl<P: LLMProvider, E: ToolExecutor> CoreLoop<P, E, NoopHooks> {
    pub fn new(provider: P, executor: E) -> Self {
        Self {
            provider,
            executor,
            hooks: NoopHooks,
            config: LoopConfig::default(),
            tools: Vec::new(),
            poll_control: None,
            history_observer: None,
            context_observer: None,
            last_run_usage: None,
            last_history: Vec::new(),
        }
    }
}

impl<P: LLMProvider, E: ToolExecutor, H: LoopHooks> CoreLoop<P, E, H> {
    pub fn with_config(mut self, config: LoopConfig) -> Self {
        self.config = config;
        self
    }

    pub fn with_tools(mut self, tools: Vec<Value>) -> Self {
        self.tools = tools;
        self
    }

    pub fn with_hooks<H2: LoopHooks>(self, hooks: H2) -> CoreLoop<P, E, H2> {
        CoreLoop {
            provider: self.provider,
            executor: self.executor,
            hooks,
            config: self.config,
            tools: self.tools,
            poll_control: self.poll_control,
            history_observer: self.history_observer,
            context_observer: self.context_observer,
            last_run_usage: self.last_run_usage,
            last_history: self.last_history,
        }
    }

    pub fn with_poll_control(
        mut self,
        poll: impl FnMut(bool) -> RoundControl + Send + 'static,
    ) -> Self {
        self.poll_control = Some(Box::new(poll));
        self
    }

    pub fn with_history_observer(
        mut self,
        observer: impl FnMut(&[LLMMessage]) + Send + 'static,
    ) -> Self {
        self.history_observer = Some(Box::new(observer));
        self
    }

    fn observe_history(&mut self, history: &[LLMMessage]) {
        if let Some(observer) = &mut self.history_observer {
            observer(history);
        }
    }

    pub fn with_context_observer(
        mut self,
        observer: impl FnMut(&LoopContext, u32) + Send + 'static,
    ) -> Self {
        self.context_observer = Some(Box::new(observer));
        self
    }

    fn observe_context(&mut self, context: &LoopContext, round_index: u32) {
        if let Some(observer) = &mut self.context_observer {
            observer(context, round_index);
        }
    }

    fn poll(&mut self, drain: bool) -> RoundControl {
        match &mut self.poll_control {
            Some(poll) => poll(drain),
            None => RoundControl::default(),
        }
    }

    pub fn provider(&self) -> &P {
        &self.provider
    }

    pub fn provider_mut(&mut self) -> &mut P {
        &mut self.provider
    }

    async fn execute_tool(
        &self,
        call: &ToolCall,
        ctx: &LoopContext,
        timeout_ms: Option<u64>,
    ) -> Result<ToolResult, String> {
        let Some(timeout_ms) = timeout_ms else {
            return self.executor.execute(call, ctx).await;
        };
        match tokio::time::timeout(
            std::time::Duration::from_millis(timeout_ms),
            self.executor.execute(call, ctx),
        )
        .await
        {
            Ok(result) => result,
            Err(_) => Ok(ToolResult::fail("tool_timeout")),
        }
    }

    fn effective_tool_timeout_ms(
        &self,
        wrap_up: bool,
        run_started: std::time::Instant,
    ) -> Option<u64> {
        let mut timeout_ms = self.config.tool_timeout_ms;
        if wrap_up {
            if let Some(wrap_timeout_ms) = self.config.wrap_up_tool_timeout_ms {
                timeout_ms = Some(
                    timeout_ms
                        .map(|current| current.min(wrap_timeout_ms))
                        .unwrap_or(wrap_timeout_ms),
                );
            }
        }
        if let Some(hard_cap_ms) = self.config.wrap_up_hard_cap_ms {
            let elapsed_ms = u64::try_from(run_started.elapsed().as_millis()).unwrap_or(u64::MAX);
            let remaining_ms = hard_cap_ms.saturating_sub(elapsed_ms).max(1);
            timeout_ms = Some(
                timeout_ms
                    .map(|current| current.min(remaining_ms))
                    .unwrap_or(remaining_ms),
            );
        }
        timeout_ms
    }

    pub async fn run(&mut self, messages: Vec<LLMMessage>) -> Vec<LoopEvent> {
        let mut events = Vec::new();
        self.run_emitting(messages, |event| events.push(event))
            .await;
        events
    }

    pub async fn run_emitting<F>(&mut self, messages: Vec<LLMMessage>, mut emit: F)
    where
        F: FnMut(LoopEvent),
    {
        let mut history = messages;
        let mut ctx = LoopContext::default();
        let mut budget_state = BudgetState::default();
        let mut round_index: u32 = 0;
        let mut completion_redos: u32 = 0;
        let mut seen_tool_calls = HashSet::new();
        let mut narration_active = false;
        let mut terminal_override: Option<(String, String)> = None;
        let run_started = std::time::Instant::now();
        let mut wrap_up = false;
        let mut wrap_up_tool_rounds_used = 0_u32;
        let mut idle_cut_count = 0_u32;

        emit(LoopEvent::new(
            "stage_start",
            json!({ "model": self.provider.model(), "engine": "rust" }),
        ));

        loop {
            self.observe_context(&ctx, round_index);
            let control = self.poll(true);
            for injected in control.steers {
                history.push(LLMMessage::text("user", injected.clone()));
                emit(LoopEvent::new(
                    "steer",
                    json!({ "content": injected, "round": round_index }),
                ));
            }
            if control.cancel {
                emit(completion_event(
                    round_index,
                    "cancelled",
                    "",
                    &[],
                    ctx.consecutive_tool_errors,
                    "cancelled",
                    "cancelled by host request",
                    1.0,
                    &ctx,
                ));
                break;
            }

            if !wrap_up
                && self
                    .config
                    .soft_timeout_ms
                    .is_some_and(|limit| run_started.elapsed().as_millis() >= u128::from(limit))
            {
                wrap_up = true;
                let notice = if self.config.wrap_up_keeps_tools {
                    "[system notice] The time budget for this task is nearly exhausted. Wait for background jobs (`wait`). Write the required output files now with bash, write_file, or edit_file. If you already drafted those contents in this chat, write_file them to the named paths. If those files already exist and look complete, verify them — do not overwrite with a truncated copy. Hidden tests score those files, not this chat. Do not keep exploring."
                } else {
                    "[system notice] The time budget for this task is exhausted. Do NOT call any more tools. Summarize what you have done so far and produce the final answer now."
                };
                history.push(LLMMessage::text("user", notice));
                self.observe_history(&history);
                emit(LoopEvent::new(
                    "soft_timeout",
                    json!({
                        "round": round_index,
                        "elapsedMs": run_started.elapsed().as_millis(),
                        "softTimeoutMs": self.config.soft_timeout_ms,
                    }),
                ));
            }

            if round_index >= self.config.max_rounds && terminal_override.is_none() {
                let reason = format!("reached maxRounds={} runaway guard", self.config.max_rounds);
                let narration = if completion_redos < MAX_COMPLETION_REDOS {
                    match self
                        .hooks
                        .before_completion(&CompletionDraft {
                            status: "budget_exhausted".into(),
                            reason: reason.clone(),
                            content: String::new(),
                            round_index,
                            had_tool_calls: ctx.tool_calls_used > 0,
                            tool_calls_used: ctx.tool_calls_used,
                            tool_successes: ctx.tool_successes,
                        })
                        .await
                    {
                        HookAction::Narrate { message, reason } => Some((message, reason)),
                        HookAction::Accept | HookAction::Retry { .. } => None,
                    }
                } else {
                    None
                };
                if let Some((message, narration_reason)) = narration {
                    history.push(LLMMessage::text("user", message));
                    self.observe_history(&history);
                    emit(LoopEvent::new(
                        "hook_action",
                        json!({
                            "hook": "before_completion",
                            "action": "narrate",
                            "reason": narration_reason,
                            "round": round_index,
                        }),
                    ));
                    completion_redos += 1;
                    narration_active = true;
                    terminal_override = Some(("budget_exhausted".into(), reason));
                } else {
                    emit(LoopEvent::new(
                        "budget_exhausted",
                        json!({ "budget": "rounds", "rounds": self.config.max_rounds }),
                    ));
                    emit(completion_event(
                        round_index.saturating_sub(1),
                        "tool_calls",
                        "",
                        &[],
                        ctx.consecutive_tool_errors,
                        "budget_exhausted",
                        reason,
                        1.0,
                        &ctx,
                    ));
                    break;
                }
            }

            let step_tool_choice;
            match self.hooks.pre_step(&history).await {
                PreStepOutcome::Reject { reason } => {
                    emit(completion_event(
                        round_index,
                        "stop",
                        "",
                        &[],
                        ctx.consecutive_tool_errors,
                        "failed",
                        reason,
                        1.0,
                        &ctx,
                    ));
                    break;
                }
                PreStepOutcome::Proceed {
                    appends,
                    rewrite,
                    reason,
                    action,
                    pre_tokens,
                    post_tokens,
                    tool_choice,
                } => {
                    if let Some(replacement) = rewrite {
                        history = replacement;
                        emit(LoopEvent::new(
                            "hook_action",
                            json!({
                                "hook": "pre_step",
                                "action": action.clone(),
                                "reason": reason.clone(),
                                "round": round_index,
                                "pre_tokens": pre_tokens,
                                "post_tokens": post_tokens,
                            }),
                        ));
                    }
                    for message in appends {
                        history.push(message);
                        emit(LoopEvent::new(
                            "hook_action",
                            json!({
                                "hook": "pre_step",
                                "action": action.clone(),
                                "reason": reason.clone(),
                                "round": round_index,
                            }),
                        ));
                    }
                    step_tool_choice = tool_choice;
                }
            }

            if let Some(tool_choice) = &step_tool_choice {
                emit(LoopEvent::new(
                    "hook_action",
                    json!({
                        "hook": "pre_step",
                        "action": "tool_choice",
                        "toolChoice": tool_choice,
                        "round": round_index,
                    }),
                ));
            }

            let mut llm_attempt = 1_u32;
            let wrap_up_withholds_tools = wrap_up
                && (!self.config.wrap_up_keeps_tools
                    || (wrap_up_tool_rounds_used >= self.config.wrap_up_max_tool_rounds
                        && self.hooks.wrap_up_may_drop_tools()));
            let request_tools = if narration_active || wrap_up_withholds_tools {
                Vec::new()
            } else {
                self.tools.clone()
            };
            let chunks = loop {
                emit(LoopEvent::new(
                    "llm_request",
                    json!({ "round": round_index, "attempt": llm_attempt }),
                ));
                self.observe_history(&history);
                match self
                    .provider
                    .stream_with_options(&history, &request_tools, step_tool_choice.as_deref())
                    .await
                {
                    Ok(chunks) => break Some(chunks),
                    Err(error) => {
                        let message = error.message.clone();
                        let kind = error.kind.as_str();
                        emit(LoopEvent::new(
                            "llm_response",
                            json!({
                                "round": round_index,
                                "attempt": llm_attempt,
                                "error": message,
                                "errorKind": kind,
                            }),
                        ));
                        match self
                            .hooks
                            .on_request_error(&error, &history, round_index)
                            .await
                        {
                            RequestErrorAction::Retry {
                                delay_ms,
                                reason,
                                rewrite,
                            } => {
                                let compacted = rewrite.is_some();
                                if let Some(rewrite) = rewrite {
                                    history = rewrite;
                                    self.observe_history(&history);
                                }
                                emit(LoopEvent::new(
                                    "hook_action",
                                    json!({
                                        "hook": "on_request_error",
                                        "action": "retry",
                                        "reason": reason,
                                        "delayMs": delay_ms,
                                        "compacted": compacted,
                                        "round": round_index,
                                    }),
                                ));
                                if delay_ms > 0 {
                                    tokio::time::sleep(std::time::Duration::from_millis(delay_ms))
                                        .await;
                                }
                                llm_attempt += 1;
                            }
                            RequestErrorAction::Fail { reason } => {
                                let failure_reason = if reason.is_empty() {
                                    format!("llm stream error: {message}")
                                } else {
                                    reason
                                };
                                emit(LoopEvent::new(
                                    "error",
                                    json!({
                                        "message": message,
                                        "round": round_index,
                                        "phase": "llm_stream",
                                        "kind": kind,
                                        "provider": error.provider,
                                        "statusCode": error.status_code,
                                        "retryAfterMs": error.retry_after_ms,
                                    }),
                                ));
                                self.observe_history(&history);
                                emit(completion_event(
                                    round_index,
                                    "stop",
                                    "",
                                    &[],
                                    ctx.consecutive_tool_errors,
                                    "failed",
                                    failure_reason,
                                    0.9,
                                    &ctx,
                                ));
                                break None;
                            }
                        }
                    }
                }
            };
            let Some(chunks) = chunks else {
                break;
            };
            let mut content = String::new();
            let mut reasoning = String::new();
            let mut reasoning_details: Option<Value> = None;
            let mut tool_calls: Vec<ToolCall> = Vec::new();
            let mut token_exhausted = false;
            let mut stream_cancelled = false;
            let mut stream_soft_cut = false;
            let mut stream_idle_cut: Option<(String, u64, u64)> = None;

            for chunk in chunks {
                if let Some(finish_reason) = chunk.finish_reason.as_deref() {
                    if finish_reason == "__soft_timeout_cut__" {
                        stream_soft_cut = true;
                        break;
                    }
                    if let Some(details) = finish_reason.strip_prefix("__idle_stream_cut__:") {
                        let mut parts = details.split(':');
                        let trigger = parts.next().unwrap_or("active_ms").to_string();
                        let chars = parts
                            .next()
                            .and_then(|value| value.parse().ok())
                            .unwrap_or(0);
                        let stale_chars = parts
                            .next()
                            .and_then(|value| value.parse().ok())
                            .unwrap_or(0);
                        stream_idle_cut = Some((trigger, chars, stale_chars));
                        break;
                    }
                }
                if let Some(delta) = chunk.content_delta {
                    content.push_str(&delta);
                    emit(LoopEvent::new("content_delta", json!({ "delta": delta })));
                }
                if let Some(delta) = chunk.reasoning_delta {
                    reasoning.push_str(&delta);
                    emit(LoopEvent::new("reasoning_delta", json!({ "delta": delta })));
                }
                if let Some(details) = chunk.reasoning_details {
                    reasoning_details = Some(details);
                }
                if let Some(call) = chunk.tool_call_delta {
                    emit(LoopEvent::new(
                        "tool_call_delta",
                        json!({
                            "id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        }),
                    ));
                    tool_calls.push(call);
                }
                if let Some(usage) = chunk.usage {
                    ctx.last_prompt_tokens = usage.prompt_tokens;
                    ctx.last_prompt_transcript_len = history.len();
                    ctx.last_cached_prompt_tokens = usage.cached_prompt_tokens;
                    ctx.last_cache_creation_tokens = usage.cache_creation_tokens;
                    ctx.accumulated_prompt_tokens += usage.prompt_tokens;
                    ctx.accumulated_completion_tokens += usage.completion_tokens;
                    self.last_run_usage = Some(LLMUsage {
                        prompt_tokens: ctx.accumulated_prompt_tokens,
                        completion_tokens: ctx.accumulated_completion_tokens,
                        total_tokens: ctx.accumulated_prompt_tokens
                            + ctx.accumulated_completion_tokens,
                        cached_prompt_tokens: usage.cached_prompt_tokens,
                        cache_creation_tokens: usage.cache_creation_tokens,
                    });
                    self.observe_context(&ctx, round_index);
                    if let Some(limits) = &self.config.budget {
                        let (next, exhausted) = consume_budget(
                            &budget_state,
                            limits,
                            usage.total_tokens,
                            usage.cached_prompt_tokens,
                            false,
                            false,
                        );
                        budget_state = next;
                        if exhausted {
                            emit(LoopEvent::new(
                                "budget_exhausted",
                                json!({ "budget": "tokens", "used": budget_state.tokens_used }),
                            ));
                            emit(completion_event(
                                round_index,
                                "stop",
                                &content,
                                &tool_calls,
                                ctx.consecutive_tool_errors,
                                "budget_exhausted",
                                "token budget exceeded",
                                1.0,
                                &ctx,
                            ));
                            token_exhausted = true;
                            break;
                        }
                    }
                }
                if self.poll(false).cancel {
                    stream_cancelled = true;
                    break;
                }
            }

            if stream_soft_cut {
                if !content.trim().is_empty() {
                    history.push(LLMMessage::text("assistant", content));
                }
                if !wrap_up {
                    wrap_up = true;
                    let notice = if self.config.wrap_up_keeps_tools {
                        "[system notice] The time budget for this task is nearly exhausted. Wait for background jobs (`wait`). Write the required output files now with bash, write_file, or edit_file. If you already drafted those contents in this chat, write_file them to the named paths. If those files already exist and look complete, verify them — do not overwrite with a truncated copy. Hidden tests score those files, not this chat. Do not keep exploring."
                    } else {
                        "[system notice] The time budget for this task is exhausted. Do NOT call any more tools. Summarize what you have done so far and produce the final answer now."
                    };
                    history.push(LLMMessage::text("user", notice));
                    emit(LoopEvent::new(
                        "soft_timeout",
                        json!({
                            "round": round_index,
                            "elapsedMs": run_started.elapsed().as_millis(),
                            "softTimeoutMs": self.config.soft_timeout_ms,
                        }),
                    ));
                }
                self.observe_history(&history);
                round_index += 1;
                continue;
            }

            if let Some((trigger, chars, stale_chars)) = stream_idle_cut {
                if !content.trim().is_empty() {
                    history.push(LLMMessage::text("assistant", content));
                }
                if !history
                    .iter()
                    .any(|message| message.content_text().contains("was cut off mid-reasoning"))
                {
                    history.push(LLMMessage::text(
                        "user",
                        "[system notice] The previous reply was cut off mid-reasoning because it ran on without issuing a tool call. Do not resume that train of thought and do not re-derive it. Issue the next concrete tool call now — write the file, run the command, or check the result.",
                    ));
                }
                emit(LoopEvent::new(
                    "hook_action",
                    json!({
                        "hook": "stream",
                        "action": "idle_stream_cut",
                        "trigger": trigger,
                        "chars": chars,
                        "staleChars": stale_chars,
                        "round": round_index,
                    }),
                ));
                idle_cut_count += 1;
                if idle_cut_count >= 2 && !wrap_up {
                    wrap_up = true;
                    let notice = if self.config.wrap_up_keeps_tools {
                        "[system notice] The time budget for this task is nearly exhausted. Wait for background jobs (`wait`). Write the required output files now with bash, write_file, or edit_file. If you already drafted those contents in this chat, write_file them to the named paths. If those files already exist and look complete, verify them — do not overwrite with a truncated copy. Hidden tests score those files, not this chat. Do not keep exploring."
                    } else {
                        "[system notice] The time budget for this task is exhausted. Do NOT call any more tools. Summarize what you have done so far and produce the final answer now."
                    };
                    history.push(LLMMessage::text("user", notice));
                    emit(LoopEvent::new(
                        "soft_timeout",
                        json!({
                            "round": round_index,
                            "elapsedMs": run_started.elapsed().as_millis(),
                            "softTimeoutMs": self.config.soft_timeout_ms,
                        }),
                    ));
                }
                self.observe_history(&history);
                round_index += 1;
                continue;
            }

            if token_exhausted {
                break;
            }

            if stream_cancelled || self.poll(false).cancel {
                if !content.trim().is_empty() && tool_calls.is_empty() {
                    history.push(LLMMessage::text("assistant", content.clone()));
                }
                self.observe_history(&history);
                emit(completion_event(
                    round_index,
                    "cancelled",
                    &content,
                    &tool_calls,
                    ctx.consecutive_tool_errors,
                    "cancelled",
                    "cancelled by host request",
                    1.0,
                    &ctx,
                ));
                break;
            }

            emit(LoopEvent::new(
                "llm_response",
                json!({
                    "round": round_index,
                    "attempt": llm_attempt,
                    "promptTokens": ctx.last_prompt_tokens,
                    "cachedPromptTokens": ctx.last_cached_prompt_tokens,
                }),
            ));

            if wrap_up_withholds_tools {
                tool_calls.clear();
            }

            if tool_calls.is_empty() {
                let (mut status, mut reason, mut confidence) = if content.trim().is_empty() {
                    (
                        "failed".to_string(),
                        "no tool calls and no final response".to_string(),
                        0.75,
                    )
                } else {
                    (
                        "completed".to_string(),
                        "assistant produced final response with no pending tools".to_string(),
                        0.85,
                    )
                };
                if let Some((override_status, override_reason)) = &terminal_override {
                    status = override_status.clone();
                    reason = override_reason.clone();
                    confidence = 1.0;
                }
                if completion_redos >= MAX_COMPLETION_REDOS {
                    emit(LoopEvent::new(
                        "hook_action",
                        json!({
                            "hook": "before_completion",
                            "action": "budget_exhausted",
                            "reason": format!(
                                "completion redo budget ({MAX_COMPLETION_REDOS}) exhausted; accepting draft"
                            ),
                            "round": round_index,
                        }),
                    ));
                } else {
                    let action = self
                        .hooks
                        .before_completion(&CompletionDraft {
                            status: status.clone(),
                            reason: reason.clone(),
                            content: content.clone(),
                            round_index,
                            had_tool_calls: false,
                            tool_calls_used: ctx.tool_calls_used,
                            tool_successes: ctx.tool_successes,
                        })
                        .await;
                    match action {
                        HookAction::Retry {
                            message,
                            reason: retry_reason,
                        } => {
                            completion_redos += 1;
                            if !content.trim().is_empty() {
                                history.push(LLMMessage::text("assistant", content));
                            }
                            history.push(LLMMessage::text("user", message));
                            let retry_reason = if retry_reason.is_empty() {
                                "before_completion retry".to_string()
                            } else {
                                retry_reason
                            };
                            emit(LoopEvent::new(
                                "hook_action",
                                json!({
                                    "hook": "before_completion",
                                    "action": "retry",
                                    "reason": retry_reason.clone(),
                                    "round": round_index,
                                }),
                            ));
                            emit(LoopEvent::new(
                                "stage_complete",
                                json!({
                                    "round": round_index,
                                    "disciplineRetry": true,
                                    "reason": retry_reason,
                                }),
                            ));
                            continue;
                        }
                        HookAction::Narrate {
                            message,
                            reason: narration_reason,
                        } => {
                            completion_redos += 1;
                            history.push(LLMMessage::text("user", message));
                            self.observe_history(&history);
                            emit(LoopEvent::new(
                                "hook_action",
                                json!({
                                    "hook": "before_completion",
                                    "action": "narrate",
                                    "reason": narration_reason,
                                    "round": round_index,
                                }),
                            ));
                            narration_active = true;
                            continue;
                        }
                        HookAction::Accept => {}
                    }
                }
                if !content.trim().is_empty() {
                    history.push(LLMMessage::text("assistant", content.clone()));
                }
                self.observe_history(&history);
                emit(completion_event(
                    round_index,
                    "stop",
                    &content,
                    &[],
                    ctx.consecutive_tool_errors,
                    &status,
                    reason,
                    confidence,
                    &ctx,
                ));
                break;
            }

            let mut assistant = LLMMessage::text("assistant", content.clone());
            assistant.tool_calls = tool_calls.clone();
            if !reasoning.is_empty() {
                assistant.reasoning = Some(reasoning);
            }
            assistant.reasoning_details = reasoning_details;
            history.push(assistant);

            let mut round_images: Vec<(String, String, String)> = Vec::new();
            let mut breaker = false;
            let mut steer_interrupted = false;
            let mut narration_request = None;
            let duplicate_calls: Vec<bool> = tool_calls
                .iter()
                .map(|call| {
                    if !self.config.tool_dedup || self.executor.dedup_exempt(call) {
                        return false;
                    }
                    let fingerprint = format!(
                        "{}:{}",
                        call.name,
                        serde_json::to_string(&call.arguments).unwrap_or_default()
                    );
                    !seen_tool_calls.insert(fingerprint)
                })
                .collect();
            let parallel = self.config.parallel_tools
                && tool_calls
                    .iter()
                    .all(|call| self.executor.concurrency_safe(call));
            let tool_timeout_ms = self.effective_tool_timeout_ms(wrap_up, run_started);
            if parallel {
                for call in &tool_calls {
                    emit(LoopEvent::new(
                        "tool_call_start",
                        json!({
                            "id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        }),
                    ));
                }
            }
            let core = &*self;
            let context = &ctx;
            let mut parallel_results = if parallel {
                Some(
                    join_all(tool_calls.iter().zip(duplicate_calls.iter().copied()).map(
                        |(call, duplicate)| async move {
                            if duplicate {
                                Ok(ToolResult::fail("duplicate_call"))
                            } else {
                                core.execute_tool(call, context, tool_timeout_ms).await
                            }
                        },
                    ))
                    .await
                    .into_iter(),
                )
            } else {
                None
            };
            for (call_idx, call) in tool_calls.iter().enumerate() {
                if !parallel {
                    emit(LoopEvent::new(
                        "tool_call_start",
                        json!({
                            "id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        }),
                    ));
                }
                let mut approval_abort = false;
                let execution = match parallel_results.as_mut() {
                    Some(results) => results
                        .next()
                        .unwrap_or_else(|| Err("parallel tool result missing".into())),
                    None if duplicate_calls[call_idx] => Ok(ToolResult::fail("duplicate_call")),
                    None => self.execute_tool(call, &ctx, tool_timeout_ms).await,
                };
                let mut result = match execution {
                    Ok(result) => result,
                    Err(error) => {
                        let error = if let Some(reason) = error.strip_prefix("__approval_abort__:")
                        {
                            approval_abort = true;
                            reason.to_string()
                        } else {
                            error
                        };
                        emit(LoopEvent::new(
                            "tool_error",
                            json!({
                                "id": call.id,
                                "name": call.name,
                                "error": error,
                            }),
                        ));
                        ToolResult::fail(error)
                    }
                };
                if result.success {
                    ctx.consecutive_tool_errors = 0;
                    ctx.tool_successes += 1;
                } else {
                    ctx.consecutive_tool_errors += 1;
                }
                self.observe_context(&ctx, round_index);

                let path = result
                    .data
                    .as_ref()
                    .and_then(Value::as_object)
                    .and_then(|obj| obj.get("path"))
                    .and_then(Value::as_str)
                    .unwrap_or(&call.name)
                    .to_string();
                if let Some((b64, media_type)) = pop_result_image(&mut result) {
                    round_images.push((path, b64, media_type));
                }
                result = self.hooks.post_tool_result(result, call).await;
                ctx.tool_calls_used += 1;

                let preview = {
                    let body = result.result_content();
                    match body.char_indices().nth(300) {
                        Some((end, _)) => format!("{}…", &body[..end]),
                        None => body,
                    }
                };
                let mut data = json!({
                    "id": call.id,
                    "name": call.name,
                    "success": result.success,
                    "durationMs": 0,
                    "resultPreview": preview,
                });
                if self.config.persist_tool_results {
                    data["result"] = json!(result.result_content());
                }
                if let Some(error) = &result.error {
                    data["error"] = json!(error);
                }
                if let Some(sandbox) = result
                    .data
                    .as_ref()
                    .and_then(|value| value.get("_sandbox"))
                    .filter(|value| value.is_object())
                {
                    data["sandbox"] = sandbox.clone();
                }
                if let Some(approval) = result
                    .data
                    .as_ref()
                    .and_then(|value| value.get("_approval"))
                    .filter(|value| value.is_object())
                {
                    data["approval"] = approval.clone();
                }
                emit(LoopEvent::new("tool_call_result", data));
                history.push(LLMMessage::tool(call, &result));

                if result.terminal {
                    let status = result
                        .data
                        .as_ref()
                        .and_then(|data| data.get("terminal_status"))
                        .and_then(Value::as_str)
                        .unwrap_or("completed")
                        .to_string();
                    for skipped in tool_calls.iter().skip(call_idx + 1) {
                        let skipped_result = ToolResult::fail(
                            "[not executed: an earlier tool ended the turn. Do not claim this call produced a result.]",
                        );
                        history.push(LLMMessage::tool(skipped, &skipped_result));
                        emit(LoopEvent::new(
                            "tool_error",
                            json!({
                                "id": skipped.id,
                                "name": skipped.name,
                                "error": "terminal tool ended turn",
                            }),
                        ));
                    }
                    self.observe_history(&history);
                    emit(completion_event(
                        round_index,
                        "tool_calls",
                        &content,
                        &tool_calls,
                        ctx.consecutive_tool_errors,
                        &status,
                        "terminal tool ended turn",
                        1.0,
                        &ctx,
                    ));
                    breaker = true;
                    break;
                }

                let control = self.poll(false);
                if approval_abort || control.cancel || control.interrupt {
                    let (skip_message, skip_error) = if approval_abort {
                        (
                            "[not executed: the turn was stopped by an approval abort. Do not claim this call produced a result.]",
                            "approval aborted",
                        )
                    } else if control.cancel {
                        (
                            "[not executed: the turn was cancelled before this call ran. Do not claim this call produced a result.]",
                            "cancelled",
                        )
                    } else {
                        (
                            "[not executed: the user sent a mid-turn message and this call was skipped so the model can respond to it. Do not claim this call produced a result.]",
                            "interrupted by steer",
                        )
                    };
                    for skipped in tool_calls.iter().skip(call_idx + 1) {
                        let skipped_result = match parallel_results.as_mut() {
                            Some(results) => match results
                                .next()
                                .unwrap_or_else(|| Err("parallel tool result missing".into()))
                            {
                                Ok(result) => result,
                                Err(error) => ToolResult::fail(error),
                            },
                            None => ToolResult::fail(skip_message),
                        };
                        let error = skipped_result
                            .error
                            .as_deref()
                            .unwrap_or(skip_error)
                            .to_string();
                        history.push(LLMMessage::tool(skipped, &skipped_result));
                        emit(LoopEvent::new(
                            "tool_error",
                            json!({
                                "id": skipped.id,
                                "name": skipped.name,
                                "error": error,
                            }),
                        ));
                    }
                    if approval_abort || control.cancel {
                        self.observe_history(&history);
                        let (status, reason) = if approval_abort {
                            (
                                "failed",
                                format!(
                                    "approval aborted: {}",
                                    result.error.as_deref().unwrap_or("approval aborted")
                                ),
                            )
                        } else {
                            ("cancelled", "cancelled by host request".into())
                        };
                        emit(completion_event(
                            round_index,
                            "tool_calls",
                            &content,
                            &tool_calls,
                            ctx.consecutive_tool_errors,
                            status,
                            reason,
                            1.0,
                            &ctx,
                        ));
                        breaker = true;
                        break;
                    }
                    ctx.consecutive_tool_errors = 0;
                    steer_interrupted = true;
                    break;
                }

                if ctx.consecutive_tool_errors >= self.config.max_tool_errors {
                    let action = self
                        .hooks
                        .before_completion(&CompletionDraft {
                            status: "failed".into(),
                            reason: "too many consecutive tool errors".into(),
                            content: content.clone(),
                            round_index,
                            had_tool_calls: true,
                            tool_calls_used: ctx.tool_calls_used,
                            tool_successes: ctx.tool_successes,
                        })
                        .await;
                    if let HookAction::Narrate { message, reason } = action {
                        narration_request = Some((message, reason));
                    } else {
                        emit(completion_event(
                            round_index,
                            "tool_calls",
                            &content,
                            &tool_calls,
                            ctx.consecutive_tool_errors,
                            "failed",
                            "too many consecutive tool errors",
                            0.9,
                            &ctx,
                        ));
                        breaker = true;
                    }
                    break;
                }
            }
            if let Some((message, reason)) = narration_request {
                history.push(LLMMessage::text("user", message));
                self.observe_history(&history);
                emit(LoopEvent::new(
                    "hook_action",
                    json!({
                        "hook": "before_completion",
                        "action": "narrate",
                        "reason": reason,
                        "round": round_index,
                    }),
                ));
                completion_redos += 1;
                narration_active = true;
                ctx.consecutive_tool_errors = 0;
            }
            if breaker {
                break;
            }

            append_read_images(&mut history, &round_images);
            self.observe_history(&history);
            emit(LoopEvent::new(
                "stage_complete",
                json!({
                    "round": round_index,
                    "toolCallCount": tool_calls.len(),
                    "consecutiveToolErrors": ctx.consecutive_tool_errors,
                    "promptTokens": ctx.last_prompt_tokens,
                    "cachedPromptTokens": ctx.last_cached_prompt_tokens,
                    "cacheCreationTokens": ctx.last_cache_creation_tokens,
                }),
            ));
            emit(completion_event(
                round_index,
                "tool_calls",
                &content,
                &tool_calls,
                ctx.consecutive_tool_errors,
                "executing",
                "tool observations were produced; continue",
                0.7,
                &ctx,
            ));
            if wrap_up {
                wrap_up_tool_rounds_used += 1;
            }
            round_index += 1;
            if steer_interrupted {
                continue;
            }
        }
        self.last_history = history;
    }
}

fn pop_result_image(result: &mut ToolResult) -> Option<(String, String)> {
    let obj = result.data.as_mut()?.as_object_mut()?;
    let blob = obj.remove("_image")?;
    let image = blob.as_object()?;
    let b64 = image.get("b64")?.as_str()?.to_string();
    if b64.is_empty() {
        return None;
    }
    let media_type = image
        .get("media_type")
        .and_then(Value::as_str)
        .unwrap_or("image/png")
        .to_string();
    obj.insert("pixels".into(), json!("attached"));
    Some((b64, media_type))
}

fn append_read_images(history: &mut Vec<LLMMessage>, images: &[(String, String, String)]) {
    if images.is_empty() {
        return;
    }
    let kept = &images[..images.len().min(MAX_READ_IMAGES_PER_ROUND)];
    let mut parts = vec![ContentPart::Text(
        "Pixels from the files just read. The tool JSON is an ASCII \
         preview; look at these images for the actual contents."
            .into(),
    )];
    for (path, b64, media_type) in kept {
        if !path.is_empty() {
            parts.push(ContentPart::Text(format!("\n{path}:")));
        }
        parts.push(ContentPart::Image {
            source: b64.clone(),
            media_type: media_type.clone(),
        });
    }
    history.push(LLMMessage {
        role: "user".into(),
        content: parts,
        name: None,
        tool_call_id: None,
        tool_calls: Vec::new(),
        reasoning: None,
        reasoning_details: None,
    });
}

#[allow(clippy::too_many_arguments)]
fn completion_event(
    round_index: u32,
    finish_reason: &str,
    content: &str,
    tool_calls: &[ToolCall],
    consecutive_tool_errors: u32,
    status: &str,
    reason: impl Into<String>,
    confidence: f64,
    ctx: &LoopContext,
) -> LoopEvent {
    let names: Vec<&str> = tool_calls.iter().map(|call| call.name.as_str()).collect();
    LoopEvent::new(
        "completion",
        json!({
            "round": round_index,
            "traceStepId": format!("round_{round_index}"),
            "finishReason": finish_reason,
            "textLength": content.chars().count(),
            "toolCalls": names,
            "toolCallCount": tool_calls.len(),
            "toolErrorCount": consecutive_tool_errors,
            "status": status,
            "reason": reason.into(),
            "confidence": confidence,
            "usage": {
                "promptTokens": ctx.accumulated_prompt_tokens,
                "completionTokens": ctx.accumulated_completion_tokens,
                "totalTokens": ctx.accumulated_prompt_tokens + ctx.accumulated_completion_tokens,
                "cachedPromptTokens": ctx.last_cached_prompt_tokens,
            },
        }),
    )
}
