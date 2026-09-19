//! Ports `packages/agent-runtime/py/tests/test_loop.py`.

use async_trait::async_trait;
use pretty_assertions::assert_eq;
use serde_json::{json, Value};
use steerable_agent_runtime::{
    BudgetLimit, CompletionDraft, ContentPart, CoreLoop, HookAction, LLMError, LLMErrorKind,
    LLMMessage, LLMProvider, LLMStreamChunk, LLMUsage, LoopConfig, LoopContext, LoopEvent,
    LoopHooks, RoundControl, RouterToolExecutor, ScriptedProvider, ScriptedTurn, ToolCall,
    ToolExecutor, ToolResult, ToolRouter,
};

fn provider(scripts: Vec<ScriptedTurn>) -> ScriptedProvider {
    ScriptedProvider::new(scripts)
}

fn text_turn(content: &str) -> ScriptedTurn {
    ScriptedTurn {
        content: content.into(),
        ..ScriptedTurn::default()
    }
}

fn tool_turn(calls: Vec<ToolCall>) -> ScriptedTurn {
    ScriptedTurn {
        tool_calls: calls,
        ..ScriptedTurn::default()
    }
}

fn tc(name: &str, args: Value) -> ToolCall {
    ToolCall::new(name, args)
}

fn final_completion(events: &[LoopEvent]) -> &Value {
    events
        .iter()
        .rev()
        .find(|event| event.kind == "completion")
        .map(|event| &event.data)
        .expect("loop never emitted a completion event")
}

fn arg_i64(args: &Value, key: &str) -> i64 {
    args.get(key).and_then(Value::as_i64).unwrap_or(0)
}

struct AlwaysRetry;

#[async_trait]
impl LoopHooks for AlwaysRetry {
    async fn before_completion(&self, _draft: &CompletionDraft) -> HookAction {
        HookAction::Retry {
            message: "try again".into(),
            reason: "always".into(),
        }
    }
}

struct Exploding;

#[async_trait]
impl ToolExecutor for Exploding {
    async fn execute(&self, _call: &ToolCall, _ctx: &LoopContext) -> Result<ToolResult, String> {
        Err("executor blew up".into())
    }
}

#[tokio::test]
async fn test_no_tool_calls_completes() {
    let mut agent = CoreLoop::new(
        provider(vec![text_turn("The answer is 4.")]),
        RouterToolExecutor::new(ToolRouter::new()),
    );
    let events = agent.run(vec![LLMMessage::text("user", "2+2?")]).await;
    assert_eq!(final_completion(&events)["status"], "completed");
    let deltas: String = events
        .iter()
        .filter(|event| event.kind == "content_delta")
        .filter_map(|event| event.data["delta"].as_str())
        .collect();
    assert_eq!(deltas, "The answer is 4.");
}

#[tokio::test]
async fn test_tool_round_then_completion() {
    let mut router = ToolRouter::new();
    router.register("add", |args| async move {
        Ok(ToolResult::ok(
            json!({ "value": arg_i64(&args, "a") + arg_i64(&args, "b") }),
        ))
    });
    let mut agent = CoreLoop::new(
        provider(vec![
            tool_turn(vec![tc("add", json!({"a": 1, "b": 2}))]),
            text_turn("Sum is 3."),
        ]),
        RouterToolExecutor::new(router),
    );
    let events = agent.run(vec![LLMMessage::text("user", "add")]).await;
    assert_eq!(final_completion(&events)["status"], "completed");

    let starts: Vec<_> = events
        .iter()
        .filter(|event| event.kind == "tool_call_start")
        .collect();
    let results: Vec<_> = events
        .iter()
        .filter(|event| event.kind == "tool_call_result")
        .collect();
    assert_eq!(starts.len(), 1);
    assert_eq!(starts[0].data["name"], "add");
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].data["success"], true);

    let tool_msgs: Vec<_> = agent.provider().calls[1]
        .iter()
        .filter(|message| message.role == "tool")
        .collect();
    assert_eq!(tool_msgs.len(), 1);
    assert_eq!(tool_msgs[0].name.as_deref(), Some("add"));
    assert!(tool_msgs[0].content_text().contains("\"success\": true"));
}

#[tokio::test]
async fn test_tool_result_image_reaches_the_next_request() {
    let mut router = ToolRouter::new();
    router.register("peek", |_| async {
        Ok(ToolResult::ok(json!({
            "path": "/app/code.png",
            "content": "PNG 4x2 ASCII preview",
            "kind": "png_ascii",
            "_image": { "b64": "QUJD", "media_type": "image/png" },
        })))
    });
    let mut agent = CoreLoop::new(
        provider(vec![
            tool_turn(vec![tc("peek", json!({}))]),
            text_turn("saw it"),
        ]),
        RouterToolExecutor::new(router),
    );
    agent.run(vec![LLMMessage::text("user", "look")]).await;
    let second = &agent.provider().calls[1];
    let tool_msgs: Vec<_> = second
        .iter()
        .filter(|message| message.role == "tool")
        .collect();
    assert_eq!(tool_msgs.len(), 1);
    assert!(!tool_msgs[0].content_text().contains("_image"));
    assert!(tool_msgs[0].content_text().contains("pixels"));
    let image_idxs: Vec<usize> = second
        .iter()
        .enumerate()
        .filter(|(_, message)| message.role == "user" && message.has_image())
        .map(|(i, _)| i)
        .collect();
    assert_eq!(image_idxs.len(), 1);
    let tool_i = second
        .iter()
        .position(|message| message.role == "tool")
        .unwrap();
    assert!(tool_i < image_idxs[0]);
    let image = second[image_idxs[0]]
        .content
        .iter()
        .find_map(ContentPart::image_source);
    assert_eq!(image, Some("QUJD"));
}

#[tokio::test]
async fn test_tool_result_image_popped_before_post_tool_hook() {
    let mut router = ToolRouter::new();
    router.register("peek", |_| async {
        Ok(ToolResult::ok(json!({
            "path": "/app/code.png",
            "content": "PNG ASCII preview",
            "kind": "png_ascii",
            "_image": { "b64": "A".repeat(20_000), "media_type": "image/png" },
        })))
    });
    let mut agent = CoreLoop::new(
        provider(vec![
            tool_turn(vec![tc("peek", json!({}))]),
            text_turn("saw it"),
        ]),
        RouterToolExecutor::new(router),
    );
    agent.run(vec![LLMMessage::text("user", "look")]).await;
    let second = &agent.provider().calls[1];
    let tool_text = second
        .iter()
        .find(|message| message.role == "tool")
        .unwrap()
        .content_text();
    assert!(!tool_text.contains("_image"));
    assert!(!tool_text.contains("\"spilled\": true"));
    let expected = "A".repeat(20_000);
    let image = second
        .iter()
        .find(|message| message.role == "user" && message.has_image())
        .and_then(|message| message.content.iter().find_map(ContentPart::image_source));
    assert_eq!(image, Some(expected.as_str()));
}

#[tokio::test]
async fn test_two_read_images_share_one_user_message_after_tools() {
    let mut router = ToolRouter::new();
    router.register("peek_a", |_| async {
        Ok(ToolResult::ok(json!({
            "path": "/app/a.png",
            "content": "A",
            "_image": { "b64": "QQ==", "media_type": "image/png" },
        })))
    });
    router.register("peek_b", |_| async {
        Ok(ToolResult::ok(json!({
            "path": "/app/b.png",
            "content": "B",
            "_image": { "b64": "Qg==", "media_type": "image/png" },
        })))
    });
    let mut agent = CoreLoop::new(
        provider(vec![
            tool_turn(vec![
                tc("peek_a", json!({})).with_id("c_a"),
                tc("peek_b", json!({})).with_id("c_b"),
            ]),
            text_turn("saw both"),
        ]),
        RouterToolExecutor::new(router),
    );
    agent.run(vec![LLMMessage::text("user", "look")]).await;
    let second = &agent.provider().calls[1];
    let tool_idxs: Vec<usize> = second
        .iter()
        .enumerate()
        .filter(|(_, message)| message.role == "tool")
        .map(|(i, _)| i)
        .collect();
    let image_idxs: Vec<usize> = second
        .iter()
        .enumerate()
        .filter(|(_, message)| message.role == "user" && message.has_image())
        .map(|(i, _)| i)
        .collect();
    assert_eq!(tool_idxs.len(), 2);
    assert_eq!(image_idxs.len(), 1);
    assert!(tool_idxs[1] < image_idxs[0]);
    let sources: Vec<_> = second[image_idxs[0]]
        .content
        .iter()
        .filter_map(ContentPart::image_source)
        .collect();
    assert_eq!(sources, vec!["QQ==", "Qg=="]);
}

#[tokio::test]
async fn test_read_images_cap_at_four_per_round() {
    let mut router = ToolRouter::new();
    for i in 0..5 {
        let name = format!("peek{i}");
        let b64 = format!("IMG{i}");
        let path = format!("/app/{i}.png");
        router.register(name, move |_| {
            let b64 = b64.clone();
            let path = path.clone();
            async move {
                Ok(ToolResult::ok(json!({
                    "path": path,
                    "content": "preview",
                    "_image": { "b64": b64, "media_type": "image/png" },
                })))
            }
        });
    }
    let calls: Vec<ToolCall> = (0..5).map(|i| tc(&format!("peek{i}"), json!({}))).collect();
    let mut agent = CoreLoop::new(
        provider(vec![tool_turn(calls), text_turn("saw it")]),
        RouterToolExecutor::new(router),
    );
    agent.run(vec![LLMMessage::text("user", "look")]).await;
    let second = &agent.provider().calls[1];
    let image_msg = second
        .iter()
        .find(|message| message.role == "user" && message.has_image())
        .expect("image user message");
    let sources: Vec<_> = image_msg
        .content
        .iter()
        .filter_map(ContentPart::image_source)
        .collect();
    assert_eq!(sources, vec!["IMG0", "IMG1", "IMG2", "IMG3"]);
}

#[tokio::test]
async fn test_loop_echoes_reasoning_details_after_tools() {
    let details = json!([{ "type": "reasoning.text", "text": "need add", "index": 0 }]);
    let mut router = ToolRouter::new();
    router.register("add", |args| async move {
        Ok(ToolResult::ok(
            json!({ "value": arg_i64(&args, "a") + arg_i64(&args, "b") }),
        ))
    });
    let mut agent = CoreLoop::new(
        provider(vec![
            ScriptedTurn {
                tool_calls: vec![tc("add", json!({"a": 1, "b": 2}))],
                reasoning: Some("need add".into()),
                reasoning_details: Some(details.clone()),
                ..ScriptedTurn::default()
            },
            text_turn("Sum is 3."),
        ]),
        RouterToolExecutor::new(router),
    );
    agent.run(vec![LLMMessage::text("user", "add")]).await;
    let assistant = agent.provider().calls[1]
        .iter()
        .rev()
        .find(|message| message.role == "assistant")
        .expect("assistant");
    assert_eq!(assistant.reasoning.as_deref(), Some("need add"));
    assert_eq!(assistant.reasoning_details.as_ref(), Some(&details));
}

#[tokio::test]
async fn test_consecutive_tool_errors_trip_breaker() {
    let mut router = ToolRouter::new();
    router.register("boom", |_| async { Err("always fails".into()) });
    let mut agent = CoreLoop::new(
        provider(vec![tool_turn(vec![tc("boom", json!({}))])]),
        RouterToolExecutor::new(router),
    )
    .with_config(LoopConfig {
        max_tool_errors: 2,
        max_rounds: 10,
        ..LoopConfig::default()
    });
    let events = agent.run(vec![LLMMessage::text("user", "go")]).await;
    let decision = final_completion(&events);
    assert_eq!(decision["status"], "failed");
    assert!(decision["reason"]
        .as_str()
        .unwrap()
        .contains("consecutive tool errors"));
    let failed = events
        .iter()
        .filter(|event| event.kind == "tool_call_result" && event.data["success"] == false)
        .count();
    assert_eq!(failed, 2);
}

#[tokio::test]
async fn test_max_rounds_runaway_guard() {
    let mut router = ToolRouter::new();
    router.register("ping", |_| async {
        Ok(ToolResult::ok(json!({ "value": "pong" })))
    });
    let mut agent = CoreLoop::new(
        provider(vec![tool_turn(vec![tc("ping", json!({}))])]),
        RouterToolExecutor::new(router),
    )
    .with_config(LoopConfig {
        max_rounds: 3,
        ..LoopConfig::default()
    });
    let events = agent.run(vec![LLMMessage::text("user", "go")]).await;
    let decision = final_completion(&events);
    assert_eq!(decision["status"], "budget_exhausted");
    assert!(decision["reason"].as_str().unwrap().contains("maxRounds"));
}

#[tokio::test]
async fn test_token_budget_exhausted() {
    let mut agent = CoreLoop::new(
        provider(vec![ScriptedTurn {
            content: "hi".into(),
            usage: Some(LLMUsage {
                prompt_tokens: 10,
                completion_tokens: 5,
                total_tokens: 15,
                ..LLMUsage::default()
            }),
            ..ScriptedTurn::default()
        }]),
        RouterToolExecutor::new(ToolRouter::new()),
    )
    .with_config(LoopConfig {
        budget: Some(BudgetLimit::new(10, 100, 100)),
        ..LoopConfig::default()
    });
    let events = agent.run(vec![LLMMessage::text("user", "hi")]).await;
    assert_eq!(final_completion(&events)["status"], "budget_exhausted");
    assert!(events
        .iter()
        .any(|event| { event.kind == "budget_exhausted" && event.data["budget"] == "tokens" }));
}

#[tokio::test]
async fn test_cache_served_prompt_tokens_are_discounted() {
    let mut agent = CoreLoop::new(
        provider(vec![ScriptedTurn {
            content: "hi".into(),
            usage: Some(LLMUsage {
                prompt_tokens: 12,
                completion_tokens: 3,
                total_tokens: 15,
                cached_prompt_tokens: 10,
                ..LLMUsage::default()
            }),
            ..ScriptedTurn::default()
        }]),
        RouterToolExecutor::new(ToolRouter::new()),
    )
    .with_config(LoopConfig {
        budget: Some(BudgetLimit::new(10, 100, 100)),
        ..LoopConfig::default()
    });
    let events = agent.run(vec![LLMMessage::text("user", "hi")]).await;
    assert_eq!(final_completion(&events)["status"], "completed");
    assert!(!events.iter().any(|event| event.kind == "budget_exhausted"));
}

#[tokio::test]
async fn test_tool_exception_is_surfaced_not_raised() {
    let mut agent = CoreLoop::new(
        provider(vec![
            tool_turn(vec![tc("explode", json!({}))]),
            text_turn("recovered"),
        ]),
        Exploding,
    );
    let events = agent.run(vec![LLMMessage::text("user", "go")]).await;
    assert!(events.iter().any(|event| event.kind == "tool_error"));
    assert_eq!(final_completion(&events)["status"], "completed");
}

#[tokio::test]
async fn test_stage_complete_emitted_after_tool_rounds() {
    let mut router = ToolRouter::new();
    router.register("echo", |args| async move {
        Ok(ToolResult::ok(json!({
            "value": args.get("text").and_then(Value::as_str).unwrap_or(""),
        })))
    });
    let mut agent = CoreLoop::new(
        provider(vec![
            tool_turn(vec![tc("echo", json!({"text": "hi"}))]),
            text_turn("done"),
        ]),
        RouterToolExecutor::new(router),
    );
    let events = agent.run(vec![LLMMessage::text("user", "hi")]).await;
    let kinds: Vec<&str> = events.iter().map(|event| event.kind.as_str()).collect();
    let result_at = kinds
        .iter()
        .position(|kind| *kind == "tool_call_result")
        .unwrap();
    let stage_at = kinds
        .iter()
        .position(|kind| *kind == "stage_complete")
        .unwrap();
    let completion_at = kinds.iter().position(|kind| *kind == "completion").unwrap();
    assert!(result_at < stage_at);
    assert!(stage_at < completion_at);
}

#[tokio::test]
async fn test_before_completion_retry_is_capped() {
    let mut agent = CoreLoop::new(
        provider(vec![text_turn("ok")]),
        RouterToolExecutor::new(ToolRouter::new()),
    )
    .with_hooks(AlwaysRetry)
    .with_config(LoopConfig {
        max_rounds: 100,
        ..LoopConfig::default()
    });
    let events = agent.run(vec![LLMMessage::text("user", "hi")]).await;
    let retries = events
        .iter()
        .filter(|event| event.kind == "hook_action" && event.data["action"] == "retry")
        .count();
    assert_eq!(retries, 32);
    assert_eq!(final_completion(&events)["status"], "completed");
}

#[tokio::test]
async fn test_cancel_before_run_skips_llm() {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    let cancel = Arc::new(AtomicBool::new(true));
    let flag = cancel.clone();
    let mut agent = CoreLoop::new(
        provider(vec![text_turn("never reached")]),
        RouterToolExecutor::new(ToolRouter::new()),
    )
    .with_poll_control(move |_drain| RoundControl {
        cancel: flag.load(Ordering::SeqCst),
        steers: Vec::new(),
        ..RoundControl::default()
    });
    let events = agent.run(vec![LLMMessage::text("user", "hi")]).await;
    assert_eq!(final_completion(&events)["status"], "cancelled");
    assert!(agent.provider().calls.is_empty());
    assert_eq!(events[0].data["engine"], "rust");
}

#[tokio::test]
async fn test_steer_lands_before_first_llm_request() {
    use std::sync::Arc;
    use std::sync::Mutex;

    let pending = Arc::new(Mutex::new(vec!["提前补充".to_string()]));
    let inbox = pending.clone();
    let mut agent = CoreLoop::new(
        provider(vec![text_turn("ok")]),
        RouterToolExecutor::new(ToolRouter::new()),
    )
    .with_poll_control(move |drain| {
        let steers = if drain {
            inbox.lock().expect("inbox").drain(..).collect()
        } else {
            Vec::new()
        };
        RoundControl {
            cancel: false,
            steers,
            ..RoundControl::default()
        }
    });
    agent.run(vec![LLMMessage::text("user", "hi")]).await;
    let first = &agent.provider().calls[0];
    assert_eq!(first.last().unwrap().content_text(), "提前补充");
}

struct ToolCaptureProvider {
    seen: std::sync::Arc<std::sync::Mutex<Vec<Value>>>,
}

#[async_trait]
impl LLMProvider for ToolCaptureProvider {
    fn name(&self) -> &str {
        "capture"
    }

    fn model(&self) -> &str {
        "capture"
    }

    async fn stream(
        &mut self,
        _messages: &[LLMMessage],
    ) -> Result<Vec<LLMStreamChunk>, steerable_agent_runtime::LLMError> {
        Ok(vec![
            LLMStreamChunk {
                content_delta: Some("done".into()),
                ..LLMStreamChunk::default()
            },
            LLMStreamChunk {
                finish_reason: Some("stop".into()),
                ..LLMStreamChunk::default()
            },
        ])
    }

    async fn stream_with_tools(
        &mut self,
        messages: &[LLMMessage],
        tools: &[Value],
    ) -> Result<Vec<LLMStreamChunk>, steerable_agent_runtime::LLMError> {
        *self.seen.lock().expect("seen tools") = tools.to_vec();
        self.stream(messages).await
    }
}

#[tokio::test]
async fn test_model_request_receives_registered_tools() {
    let seen = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let mut loop_ = CoreLoop::new(
        ToolCaptureProvider {
            seen: std::sync::Arc::clone(&seen),
        },
        RouterToolExecutor::new(ToolRouter::new()),
    )
    .with_tools(vec![json!({
        "type": "function",
        "function": {"name": "todo_write", "parameters": {"type": "object"}},
    })]);

    loop_.run(vec![LLMMessage::text("user", "plan")]).await;

    assert_eq!(
        seen.lock().expect("seen tools")[0]["function"]["name"],
        "todo_write"
    );
}

struct FailingProvider;

#[async_trait]
impl LLMProvider for FailingProvider {
    fn name(&self) -> &str {
        "failing"
    }

    fn model(&self) -> &str {
        "failing-model"
    }

    async fn stream(&mut self, _messages: &[LLMMessage]) -> Result<Vec<LLMStreamChunk>, LLMError> {
        Err(LLMError {
            message: "failing: HTTP 401 (auth)".into(),
            kind: LLMErrorKind::Auth,
            status_code: Some(401),
            provider: Some("failing".into()),
            retry_after_ms: None,
        })
    }
}

#[tokio::test]
async fn test_provider_error_emits_error_and_failed_completion() {
    let mut loop_ = CoreLoop::new(FailingProvider, RouterToolExecutor::new(ToolRouter::new()));

    let events = loop_.run(vec![LLMMessage::text("user", "hello")]).await;

    assert_eq!(
        events
            .iter()
            .map(|event| event.kind.as_str())
            .collect::<Vec<_>>(),
        vec![
            "stage_start",
            "llm_request",
            "llm_response",
            "error",
            "completion"
        ]
    );
    assert_eq!(
        events[3].data,
        json!({
            "message": "failing: HTTP 401 (auth)",
            "round": 0,
            "phase": "llm_stream",
            "kind": "auth",
            "provider": "failing",
            "statusCode": 401,
            "retryAfterMs": null,
        })
    );
    assert_eq!(events[4].data["status"], "failed");
    assert_eq!(events[4].data["reason"], "failing: HTTP 401 (auth)");
}

struct SlowToolExecutor;

#[async_trait]
impl ToolExecutor for SlowToolExecutor {
    async fn execute(&self, call: &ToolCall, _ctx: &LoopContext) -> Result<ToolResult, String> {
        let delay_ms = if call.name == "slow" { 20 } else { 100 };
        tokio::time::sleep(std::time::Duration::from_millis(delay_ms)).await;
        Ok(ToolResult::ok(json!({"name": call.name})))
    }
}

#[tokio::test]
async fn test_run_hard_cap_limits_tools_from_start() {
    let mut loop_ = CoreLoop::new(
        provider(vec![
            tool_turn(vec![tc("hang", json!({}))]),
            text_turn("recovered"),
        ]),
        SlowToolExecutor,
    )
    .with_config(LoopConfig {
        tool_timeout_ms: Some(60_000),
        wrap_up_hard_cap_ms: Some(10),
        ..LoopConfig::default()
    });

    let events = loop_.run(vec![LLMMessage::text("user", "go")]).await;

    let result = events
        .iter()
        .find(|event| event.kind == "tool_call_result")
        .unwrap();
    assert_eq!(result.data["error"], "tool_timeout");
    assert_eq!(events.last().unwrap().data["status"], "completed");
}

#[tokio::test]
async fn test_wrap_up_uses_shorter_tool_timeout() {
    let mut loop_ = CoreLoop::new(
        provider(vec![
            tool_turn(vec![tc("slow", json!({}))]),
            tool_turn(vec![tc("hang", json!({}))]),
            text_turn("recovered"),
        ]),
        SlowToolExecutor,
    )
    .with_tools(vec![json!({
        "type": "function",
        "function": {"name": "hang", "parameters": {"type": "object"}},
    })])
    .with_config(LoopConfig {
        soft_timeout_ms: Some(5),
        wrap_up_keeps_tools: true,
        wrap_up_max_tool_rounds: 1,
        tool_timeout_ms: Some(60_000),
        wrap_up_tool_timeout_ms: Some(10),
        ..LoopConfig::default()
    });

    let events = loop_.run(vec![LLMMessage::text("user", "go")]).await;

    let results = events
        .iter()
        .filter(|event| event.kind == "tool_call_result")
        .collect::<Vec<_>>();
    assert_eq!(results.len(), 2);
    assert_eq!(results[1].data["error"], "tool_timeout");
    assert_eq!(events.last().unwrap().data["status"], "completed");
}
