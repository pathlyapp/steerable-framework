//! PyO3 entry: Python owns provider/executor/hooks; Rust owns the loop.

use async_trait::async_trait;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyModule;
use serde_json::Value;

use crate::engine::{CoreLoop, LoopConfig, RoundControl};
use crate::errors::{LLMError, LLMErrorKind};
use crate::executor::ToolExecutor;
use crate::hooks::{CompletionDraft, HookAction, LoopHooks, PreStepOutcome, RequestErrorAction};
use crate::provider::{LLMProvider, LLMStreamChunk};
use crate::types::{LLMMessage, LoopContext, LoopEvent, ToolCall, ToolResult};
use crate::wire::{
    chunks_from_value, config_from_value, messages_from_value, messages_to_value,
    tool_call_to_value, tool_result_from_value, tool_result_to_value, usage_to_value,
};

struct CallbackProvider {
    name: String,
    model: String,
    stream_llm: Py<PyAny>,
}

#[async_trait]
impl LLMProvider for CallbackProvider {
    fn name(&self) -> &str {
        &self.name
    }

    fn model(&self) -> &str {
        &self.model
    }

    async fn stream(&mut self, messages: &[LLMMessage]) -> Result<Vec<LLMStreamChunk>, LLMError> {
        self.stream_with_options(messages, &[], None).await
    }

    async fn stream_with_options(
        &mut self,
        messages: &[LLMMessage],
        tools: &[Value],
        tool_choice: Option<&str>,
    ) -> Result<Vec<LLMStreamChunk>, LLMError> {
        let payload = serde_json::json!({
            "messages": messages_to_value(messages),
            "tools_enabled": !tools.is_empty(),
            "tool_choice": tool_choice,
        })
        .to_string();
        let raw = call_python_str(&self.stream_llm, payload);
        let value: Value = serde_json::from_str(&raw).map_err(|error| {
            LLMError::new(
                format!("{}: invalid callback response: {error}", self.name),
                LLMErrorKind::Unknown,
            )
        })?;
        if value.get("ok").and_then(Value::as_bool) == Some(false) {
            let kind = match value.get("kind").and_then(Value::as_str) {
                Some("transport") => LLMErrorKind::Transport,
                Some("rate_limit") => LLMErrorKind::RateLimit,
                Some("context_overflow") => LLMErrorKind::ContextOverflow,
                Some("auth") => LLMErrorKind::Auth,
                Some("invalid_request") => LLMErrorKind::InvalidRequest,
                Some("server") => LLMErrorKind::Server,
                _ => LLMErrorKind::Unknown,
            };
            return Err(LLMError {
                message: value
                    .get("error")
                    .and_then(Value::as_str)
                    .unwrap_or("provider callback failed")
                    .to_string(),
                kind,
                status_code: value
                    .get("status_code")
                    .and_then(Value::as_u64)
                    .and_then(|status| u16::try_from(status).ok()),
                provider: value
                    .get("provider")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                retry_after_ms: value.get("retry_after_ms").and_then(Value::as_u64),
            });
        }
        chunks_from_value(&value).map_err(|error| {
            LLMError::new(
                format!("{}: invalid callback chunks: {error}", self.name),
                LLMErrorKind::Unknown,
            )
        })
    }
}

struct CallbackExecutor {
    exec_tool: Py<PyAny>,
    tool_concurrency_safe: Py<PyAny>,
    tool_dedup_exempt: Py<PyAny>,
}

#[async_trait]
impl ToolExecutor for CallbackExecutor {
    async fn execute(&self, call: &ToolCall, _ctx: &LoopContext) -> Result<ToolResult, String> {
        let payload = tool_call_to_value(call).to_string();
        let callback = Python::with_gil(|py| self.exec_tool.clone_ref(py));
        let raw = tokio::task::spawn_blocking(move || call_python_str(&callback, payload))
            .await
            .map_err(|error| error.to_string())?;
        let value: Value = serde_json::from_str(&raw).map_err(|err| err.to_string())?;
        if value.get("ok").and_then(Value::as_bool) == Some(false) {
            return Err(value
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("tool executor failed")
                .to_string());
        }
        let result = value.get("result").cloned().unwrap_or(Value::Null);
        tool_result_from_value(&result)
    }

    fn concurrency_safe(&self, call: &ToolCall) -> bool {
        let payload = tool_call_to_value(call).to_string();
        Python::with_gil(|py| {
            self.tool_concurrency_safe
                .call1(py, (payload,))
                .and_then(|value| value.extract::<bool>(py))
                .unwrap_or(false)
        })
    }

    fn dedup_exempt(&self, call: &ToolCall) -> bool {
        let payload = tool_call_to_value(call).to_string();
        Python::with_gil(|py| {
            self.tool_dedup_exempt
                .call1(py, (payload,))
                .and_then(|value| value.extract::<bool>(py))
                .unwrap_or(false)
        })
    }
}

struct CallbackHooks {
    before_completion: Py<PyAny>,
    post_tool_result: Py<PyAny>,
    pre_step: Py<PyAny>,
    on_request_error: Py<PyAny>,
    wrap_up_may_drop_tools: Py<PyAny>,
}

#[async_trait]
impl LoopHooks for CallbackHooks {
    async fn before_completion(&self, draft: &CompletionDraft) -> HookAction {
        let payload = serde_json::json!({
            "status": draft.status,
            "reason": draft.reason,
            "content": draft.content,
            "round_index": draft.round_index,
            "had_tool_calls": draft.had_tool_calls,
            "tool_calls_used": draft.tool_calls_used,
            "tool_successes": draft.tool_successes,
        })
        .to_string();
        let raw = call_python_str(&self.before_completion, payload);
        let value: Value = serde_json::from_str(&raw).unwrap_or(Value::Null);
        match value.get("kind").and_then(Value::as_str) {
            Some("retry") => HookAction::Retry {
                message: value
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                reason: value
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
            },
            Some("narrate") => HookAction::Narrate {
                message: value
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                reason: value
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
            },
            _ => HookAction::Accept,
        }
    }

    async fn post_tool_result(&self, result: ToolResult, call: &ToolCall) -> ToolResult {
        let payload = serde_json::json!({
            "result": tool_result_to_value(&result),
            "call": tool_call_to_value(call),
        })
        .to_string();
        let raw = call_python_str(&self.post_tool_result, payload);
        let value: Value = serde_json::from_str(&raw).unwrap_or(Value::Null);
        tool_result_from_value(&value).unwrap_or(result)
    }

    async fn pre_step(&self, transcript: &[LLMMessage]) -> PreStepOutcome {
        let payload = messages_to_value(transcript).to_string();
        let raw = call_python_str(&self.pre_step, payload);
        let value: Value = serde_json::from_str(&raw).unwrap_or(Value::Null);
        if value.get("ok").and_then(Value::as_bool) == Some(false) {
            return PreStepOutcome::Reject {
                reason: value
                    .get("error")
                    .and_then(Value::as_str)
                    .unwrap_or("pre_step callback failed")
                    .to_string(),
            };
        }
        if value.get("kind").and_then(Value::as_str) == Some("reject") {
            return PreStepOutcome::Reject {
                reason: value
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("rejected by pre_step hook")
                    .to_string(),
            };
        }
        let appends = value
            .get("appends")
            .map(messages_from_value)
            .and_then(Result::ok)
            .unwrap_or_default();
        let rewrite = value
            .get("rewrite")
            .filter(|item| !item.is_null())
            .map(messages_from_value)
            .and_then(Result::ok);
        PreStepOutcome::Proceed {
            appends,
            rewrite,
            reason: value
                .get("reason")
                .and_then(Value::as_str)
                .map(str::to_string),
            action: value
                .get("action")
                .and_then(Value::as_str)
                .unwrap_or("append")
                .to_string(),
            pre_tokens: value.get("pre_tokens").and_then(Value::as_i64),
            post_tokens: value.get("post_tokens").and_then(Value::as_i64),
            tool_choice: value
                .get("tool_choice")
                .and_then(Value::as_str)
                .map(str::to_string),
        }
    }

    async fn on_request_error(
        &self,
        error: &LLMError,
        transcript: &[LLMMessage],
        round_index: u32,
    ) -> RequestErrorAction {
        let payload = serde_json::json!({
            "error": {
                "message": error.message.clone(),
                "kind": error.kind.as_str(),
                "status_code": error.status_code,
                "provider": error.provider.clone(),
                "retry_after_ms": error.retry_after_ms,
            },
            "transcript": messages_to_value(transcript),
            "round_index": round_index,
        })
        .to_string();
        let raw = call_python_str(&self.on_request_error, payload);
        let value: Value = serde_json::from_str(&raw).unwrap_or(Value::Null);
        if value.get("kind").and_then(Value::as_str) != Some("retry") {
            return RequestErrorAction::Fail {
                reason: value
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or(&error.message)
                    .to_string(),
            };
        }
        RequestErrorAction::Retry {
            delay_ms: value.get("delay_ms").and_then(Value::as_u64).unwrap_or(0),
            reason: value
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            rewrite: value
                .get("rewrite")
                .map(messages_from_value)
                .and_then(Result::ok),
        }
    }

    fn wrap_up_may_drop_tools(&self) -> bool {
        Python::with_gil(|py| {
            self.wrap_up_may_drop_tools
                .call0(py)
                .and_then(|value| value.extract::<bool>(py))
                .unwrap_or(true)
        })
    }
}

fn call_python_str(cb: &Py<PyAny>, payload: String) -> String {
    Python::with_gil(|py| match cb.call1(py, (payload,)) {
        Ok(obj) => obj.extract::<String>(py).unwrap_or_default(),
        Err(err) => format!(
            r#"{{"ok": false, "error": {}}}"#,
            json_escape(&err.to_string())
        ),
    })
}

fn json_escape(text: &str) -> String {
    serde_json::to_string(text).unwrap_or_else(|_| "\"callback failed\"".into())
}

fn emit_python(emit: &Py<PyAny>, event: LoopEvent) {
    Python::with_gil(|py| {
        let _ = emit.call1(py, (event.kind.as_str(), event.data.to_string()));
    });
}

#[pyfunction]
fn run_turn(
    py: Python<'_>,
    config_json: &str,
    messages_json: &str,
    tools_json: &str,
    provider_name: &str,
    provider_model: &str,
    emit: Py<PyAny>,
    stream_llm: Py<PyAny>,
    exec_tool: Py<PyAny>,
    tool_concurrency_safe: Py<PyAny>,
    tool_dedup_exempt: Py<PyAny>,
    sync_history: Py<PyAny>,
    sync_context: Py<PyAny>,
    before_completion: Py<PyAny>,
    post_tool_result: Py<PyAny>,
    poll_control: Py<PyAny>,
    pre_step: Py<PyAny>,
    on_request_error: Py<PyAny>,
    wrap_up_may_drop_tools: Py<PyAny>,
) -> PyResult<String> {
    let config_value: Value = serde_json::from_str(config_json)
        .map_err(|err| PyRuntimeError::new_err(err.to_string()))?;
    let messages_value: Value = serde_json::from_str(messages_json)
        .map_err(|err| PyRuntimeError::new_err(err.to_string()))?;
    let tools_value: Value =
        serde_json::from_str(tools_json).map_err(|err| PyRuntimeError::new_err(err.to_string()))?;
    let config: LoopConfig = config_from_value(&config_value);
    let messages = messages_from_value(&messages_value).map_err(PyRuntimeError::new_err)?;
    let tools = tools_value.as_array().cloned().unwrap_or_default();
    let mut core = CoreLoop::new(
        CallbackProvider {
            name: provider_name.to_string(),
            model: provider_model.to_string(),
            stream_llm,
        },
        CallbackExecutor {
            exec_tool,
            tool_concurrency_safe,
            tool_dedup_exempt,
        },
    )
    .with_config(config)
    .with_tools(tools)
    .with_hooks(CallbackHooks {
        before_completion,
        post_tool_result,
        pre_step,
        on_request_error,
        wrap_up_may_drop_tools,
    })
    .with_poll_control(move |drain| {
        let raw = call_python_str(
            &poll_control,
            serde_json::json!({ "drain": drain }).to_string(),
        );
        let value: Value = serde_json::from_str(&raw).unwrap_or(Value::Null);
        let steers = value
            .get("steers")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default();
        RoundControl {
            cancel: value
                .get("cancel")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            interrupt: value
                .get("interrupt")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            steers,
        }
    })
    .with_history_observer(move |messages| {
        let _ = call_python_str(&sync_history, messages_to_value(messages).to_string());
    })
    .with_context_observer(move |context, round_index| {
        let payload = serde_json::json!({
            "round_index": round_index,
            "tool_calls_used": context.tool_calls_used,
            "tool_successes": context.tool_successes,
            "consecutive_tool_errors": context.consecutive_tool_errors,
            "last_prompt_tokens": context.last_prompt_tokens,
            "last_prompt_transcript_len": context.last_prompt_transcript_len,
            "last_cached_prompt_tokens": context.last_cached_prompt_tokens,
            "last_cache_creation_tokens": context.last_cache_creation_tokens,
            "accumulated_prompt_tokens": context.accumulated_prompt_tokens,
            "accumulated_completion_tokens": context.accumulated_completion_tokens,
        });
        let _ = call_python_str(&sync_context, payload.to_string());
    });
    py.allow_threads(|| {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|err| PyRuntimeError::new_err(err.to_string()))?;
        rt.block_on(async {
            core.run_emitting(messages, |event| emit_python(&emit, event))
                .await;
        });
        let mut result = match &core.last_run_usage {
            Some(usage) => usage_to_value(usage),
            None => serde_json::json!({}),
        };
        result["history"] = messages_to_value(&core.last_history);
        Ok(result.to_string())
    })
}

#[pymodule]
fn steerable_agent_runtime_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_turn, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
