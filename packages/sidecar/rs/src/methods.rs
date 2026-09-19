//! Catalog RPC methods and in-memory session/tool handlers.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{LazyLock, Mutex};
use std::time::Instant;

use serde_json::{json, Map, Value};
use steerable_agent_runtime::{
    describe_catalog_providers, describe_compat_flags, describe_provider_presets, list_skills_rpc,
    preset_for, ptc_js_enabled, run_code_enabled, run_code_tool_descriptor, run_js_tool_descriptor,
    source_cap_error, todo_write_result, todo_write_tool_descriptor, tool_descriptor,
    wait_js_tool_descriptor, web_fetch_live, web_fetch_schema, web_search_live, web_search_schema,
    AnthropicProvider, CoreLoop, GoogleGenAIProvider, LLMMessage, LoopEvent, OpenAICompatProvider,
    OpenAIResponsesProvider, RouterToolExecutor, ScriptedProvider, ScriptedTurn, TodoStore,
    ToolResult, ToolRouter, WebToolsConfig, RUN_CODE, RUN_JS, TODO_TOOL_NAME, WAIT_JS, WEB_FETCH,
    WEB_FETCH_DESCRIPTION, WEB_SEARCH, WEB_SEARCH_DESCRIPTION,
};

use crate::ptc_js_child::{invoke_run_js_live, invoke_wait_js_live};
use crate::rpc;
use crate::run_code_child::invoke_run_code_child;

pub const PROTOCOL_VERSION: &str = "0.1.0";
pub const SIDECAR_VERSION: &str = env!("CARGO_PKG_VERSION");

pub const METHODS: &[&str] = &[
    "system.ping",
    "system.shutdown",
    "system.shutdown_now",
    "agent.session.create",
    "agent.session.resume",
    "agent.session.list",
    "tool.list",
    "tool.invoke",
    "workspace.apply_edits",
    "skills.list",
    "trace.fetch",
    "trace.export",
    "config.get",
    "config.set",
    "compat.describe",
    "sandbox.describe",
    "plugin.list",
    "plugin.enable",
    "plugin.disable",
    "plugin.reload",
    "presets.describe",
    "presets.resolve",
    "catalog.describe",
    "models.list",
    "harness.describe",
    "agent.chat.stream",
    "agent.chat.cancel",
    "agent.chat.steer",
    "agent.chat.compact",
    "agent.chat.fork",
    "agent.session.fork",
    "agent.session.branches",
    "agent.session.tree",
    "agent.session.messages",
];

pub struct SidecarState {
    started: Instant,
    sessions: HashMap<String, Value>,
    next_stream: u64,
    pub shutdown: bool,
}

impl SidecarState {
    pub fn new() -> Self {
        Self {
            started: Instant::now(),
            sessions: HashMap::new(),
            next_stream: 0,
            shutdown: false,
        }
    }

    pub fn health(&self) -> Value {
        let uptime = self.started.elapsed().as_millis() as u64;
        json!({
            "status": "ok",
            "version": SIDECAR_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
            "uptimeMs": uptime,
            "pid": std::process::id(),
            "pythonVersion": "rust",
            "platform": format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH),
            "loadedProviders": ["openai_compat", "openai_responses", "anthropic", "google_genai", "ollama"],
            "loadedTools": 0,
            "activeTraces": 0,
            "checks": {
                "engine": {"status": "ok", "message": "rust"}
            }
        })
    }
}

pub struct TurnLlm {
    pub provider: String,
    pub base_url: Option<String>,
    pub api_key: Option<String>,
    pub model: Option<String>,
    pub temperature: Option<f64>,
    pub max_tokens: Option<i64>,
    pub reasoning_effort: Option<String>,
}

impl Default for TurnLlm {
    fn default() -> Self {
        Self {
            provider: "openai_compat".into(),
            base_url: None,
            api_key: None,
            model: None,
            temperature: None,
            max_tokens: None,
            reasoning_effort: None,
        }
    }
}

pub enum Dispatch {
    Reply(Value),
    Stream {
        reply: Value,
        stream_id: String,
        messages: Vec<LLMMessage>,
        llm: TurnLlm,
    },
    Async(Pin<Box<dyn Future<Output = Value> + Send>>),
}

pub fn dispatch(state: &mut SidecarState, request: &Value) -> Dispatch {
    let id = request.get("id").cloned().unwrap_or(Value::Null);
    let Some(method) = request.get("method").and_then(Value::as_str) else {
        return Dispatch::Reply(rpc::response_error(
            &id,
            -32600,
            "invalid request",
            "invalid_request",
        ));
    };
    if !METHODS.contains(&method) {
        return Dispatch::Reply(rpc::response_error(
            &id,
            -32601,
            &format!("method not found: {method}"),
            "method_not_found",
        ));
    }
    let params = request.get("params").cloned().unwrap_or(json!({}));
    match method {
        "system.ping" => Dispatch::Reply(rpc::response_ok(&id, state.health())),
        "system.shutdown" | "system.shutdown_now" => {
            state.shutdown = true;
            Dispatch::Reply(rpc::response_null(&id))
        }
        "agent.session.create" => {
            let map = rpc::object_params(&params);
            let session_id = map
                .get("sessionId")
                .and_then(Value::as_str)
                .map(str::to_string)
                .unwrap_or_else(|| format!("ses_{}", state.sessions.len() + 1));
            let chat_id = map
                .get("chatId")
                .cloned()
                .unwrap_or_else(|| json!("chat_local"));
            let session = json!({
                "sessionId": session_id.clone(),
                "userId": map.get("userId").cloned().unwrap_or_else(|| json!("local")),
                "chatId": chat_id,
                "currentStage": map.get("currentStage").cloned().unwrap_or_else(|| json!("plan")),
                "isActive": true,
            });
            state.sessions.insert(session_id, session.clone());
            Dispatch::Reply(rpc::response_ok(&id, session))
        }
        "agent.session.resume" => {
            let map = rpc::object_params(&params);
            let Some(session_id) = map.get("sessionId").and_then(Value::as_str) else {
                return Dispatch::Reply(rpc::response_error(
                    &id,
                    -32602,
                    "sessionId required",
                    "invalid_params",
                ));
            };
            match state.sessions.get(session_id) {
                Some(session) => Dispatch::Reply(rpc::response_ok(&id, session.clone())),
                None => Dispatch::Reply(rpc::response_error(
                    &id,
                    -32004,
                    "session not found",
                    "not_found",
                )),
            }
        }
        "agent.session.list" => {
            let listed: Vec<Value> = state.sessions.values().cloned().collect();
            Dispatch::Reply(rpc::response_ok(&id, json!(listed)))
        }
        "tool.list" => Dispatch::Reply(rpc::response_ok(&id, json!(listed_tools()))),
        "tool.invoke" => {
            let map = rpc::object_params(&params);
            let Some(name) = map.get("name").and_then(Value::as_str).map(str::to_string) else {
                return Dispatch::Reply(rpc::response_error(
                    &id,
                    -32602,
                    "name required",
                    "invalid_params",
                ));
            };
            let args = map.get("arguments").cloned().unwrap_or_else(|| json!({}));
            let id = id.clone();
            Dispatch::Async(Box::pin(async move {
                let result = invoke_builtin(&name, &args).await;
                rpc::response_ok(&id, result.to_rpc())
            }))
        }
        "skills.list" => match list_skills_rpc(&params) {
            Ok(result) => Dispatch::Reply(rpc::response_ok(&id, result)),
            Err((code, message, kind)) => {
                Dispatch::Reply(rpc::response_error(&id, code, &message, kind))
            }
        },
        "plugin.list" => Dispatch::Reply(rpc::response_ok(&id, json!([]))),
        "models.list" => Dispatch::Reply(rpc::response_ok(&id, json!({"models": []}))),
        "config.get" => Dispatch::Reply(rpc::response_ok(&id, json!({}))),
        "config.set" => Dispatch::Reply(rpc::response_null(&id)),
        "compat.describe" => Dispatch::Reply(rpc::response_ok(
            &id,
            json!({"flags": describe_compat_flags()}),
        )),
        "sandbox.describe" => {
            let network = params
                .get("network")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let hosts: Option<Vec<String>> = params
                .get("allowedHosts")
                .and_then(Value::as_array)
                .map(|arr| {
                    arr.iter()
                        .filter_map(|value| value.as_str().map(str::to_string))
                        .collect::<Vec<_>>()
                })
                .filter(|hosts| !hosts.is_empty());
            Dispatch::Reply(rpc::response_ok(
                &id,
                crate::sandbox::describe_exec_sandbox(network, hosts.as_deref()),
            ))
        }
        "presets.describe" => Dispatch::Reply(rpc::response_ok(
            &id,
            json!({"presets": describe_provider_presets()}),
        )),
        "presets.resolve" => {
            let base_url = params.get("baseUrl").and_then(Value::as_str).unwrap_or("");
            let model = params.get("model").and_then(Value::as_str).unwrap_or("");
            Dispatch::Reply(rpc::response_ok(
                &id,
                json!({"preset": preset_for(base_url, model).map(|preset| preset.to_wire())}),
            ))
        }
        "catalog.describe" => Dispatch::Reply(rpc::response_ok(
            &id,
            json!({"providers": describe_catalog_providers()}),
        )),
        "harness.describe" => Dispatch::Reply(rpc::response_ok(
            &id,
            json!({"engine": "rust", "version": SIDECAR_VERSION}),
        )),
        "trace.fetch" => Dispatch::Reply(rpc::response_ok(
            &id,
            json!({"trace": null, "spans": [], "events": []}),
        )),
        "trace.export" => Dispatch::Reply(rpc::response_ok(&id, json!({"status": "skipped"}))),
        "workspace.apply_edits" => match crate::file_edit::apply_edits_rpc(&params) {
            Ok(result) => Dispatch::Reply(rpc::response_ok(&id, result)),
            Err((code, message, kind, data)) if data.is_null() => {
                Dispatch::Reply(rpc::response_error(&id, code, &message, kind))
            }
            Err((code, message, kind, data)) => {
                Dispatch::Reply(rpc::response_error_data(&id, code, &message, kind, data))
            }
        },
        "agent.chat.cancel" | "agent.chat.steer" | "agent.chat.compact" | "agent.chat.fork"
        | "agent.session.fork" | "plugin.enable" | "plugin.disable" | "plugin.reload" => {
            Dispatch::Reply(rpc::response_null(&id))
        }
        "agent.session.branches" => Dispatch::Reply(rpc::response_ok(
            &id,
            json!({"lineage": [], "children": []}),
        )),
        "agent.session.tree" => Dispatch::Reply(rpc::response_ok(
            &id,
            json!({"recordId": null, "tree": null, "nodeCount": 0, "truncated": false}),
        )),
        "agent.session.messages" => Dispatch::Reply(rpc::response_ok(&id, json!({"messages": []}))),
        "agent.chat.stream" => {
            let map = rpc::object_params(&params);
            let messages = parse_messages(map.get("messages"));
            state.next_stream += 1;
            let stream_id = format!("str_{}", state.next_stream);
            Dispatch::Stream {
                reply: rpc::response_ok(&id, json!({"streamId": stream_id})),
                stream_id,
                messages,
                llm: turn_llm_from_params(&map),
            }
        }
        _ => Dispatch::Reply(rpc::response_error(
            &id,
            -32601,
            &format!("method not found: {method}"),
            "method_not_found",
        )),
    }
}

pub fn parse_messages(value: Option<&Value>) -> Vec<LLMMessage> {
    let Some(Value::Array(items)) = value else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for item in items {
        let role = item
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("user")
            .to_string();
        let text = match item.get("content") {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Array(parts)) => parts
                .iter()
                .filter_map(|part| {
                    if let Some(text) = part.get("text").and_then(Value::as_str) {
                        Some(text)
                    } else {
                        part.as_str()
                    }
                })
                .collect::<Vec<_>>()
                .join(""),
            _ => String::new(),
        };
        out.push(LLMMessage::text(role, text));
    }
    out
}

pub fn chunk_from_event(stream_id: &str, event: &LoopEvent) -> Option<Value> {
    let data = &event.data;
    match event.kind.as_str() {
        "content_delta" => Some(json!({
            "streamId": stream_id,
            "delta": data.get("delta").cloned().unwrap_or(json!(""))
        })),
        "reasoning_delta" => Some(json!({
            "streamId": stream_id,
            "reasoningDelta": data.get("delta").cloned().unwrap_or(json!(""))
        })),
        "tool_call_start" => Some(json!({
            "streamId": stream_id,
            "toolCall": {
                "id": data.get("id").cloned().unwrap_or(json!("")),
                "name": data.get("name").cloned().unwrap_or(json!("")),
                "arguments": data.get("arguments").cloned().unwrap_or(json!({}))
            }
        })),
        "tool_call_result" | "tool_error" => Some(json!({
            "streamId": stream_id,
            "toolResult": data
        })),
        "steer" => Some(json!({
            "streamId": stream_id,
            "notice": {"kind": "steer", "content": data.get("content").cloned().unwrap_or(json!(""))}
        })),
        "soft_timeout" | "budget_exhausted" | "hook_action" => Some(json!({
            "streamId": stream_id,
            "notice": {"kind": event.kind, "data": data}
        })),
        "error" => None,
        _ => Some(json!({
            "streamId": stream_id,
            "notice": {"kind": event.kind, "data": data}
        })),
    }
}

static TODO_STORE: LazyLock<Mutex<TodoStore>> = LazyLock::new(|| Mutex::new(TodoStore::default()));

fn process_environ() -> HashMap<String, String> {
    std::env::vars().collect()
}

fn web_config() -> WebToolsConfig {
    WebToolsConfig::from_process_env().unwrap_or_else(|error| {
        eprintln!("[sidecar] web tools config: {error}; using defaults");
        WebToolsConfig::default()
    })
}

fn listed_tools() -> Vec<Value> {
    let mut tools = vec![
        todo_write_tool_descriptor(),
        tool_descriptor(WEB_FETCH, WEB_FETCH_DESCRIPTION, web_fetch_schema()),
    ];
    if web_config().search_configured() {
        tools.push(tool_descriptor(
            WEB_SEARCH,
            WEB_SEARCH_DESCRIPTION,
            web_search_schema(),
        ));
    }
    if run_code_enabled(&process_environ()) {
        tools.push(run_code_tool_descriptor());
    }
    if ptc_js_enabled(&process_environ()) {
        tools.push(run_js_tool_descriptor());
        tools.push(wait_js_tool_descriptor());
    }
    tools
}

fn ptc_nested() -> std::sync::Arc<
    dyn Fn(String, Map<String, Value>) -> Pin<Box<dyn Future<Output = ToolResult> + Send>>
        + Send
        + Sync,
> {
    std::sync::Arc::new(|name, arguments| Box::pin(nested_run_code_tool(name, arguments)))
}

async fn ptc_js_run_invoke(args: &Value) -> ToolResult {
    let env = process_environ();
    if !ptc_js_enabled(&env) {
        return ToolResult::fail("run_js is disabled");
    }
    invoke_run_js_live(args, &env, ptc_nested()).await
}

async fn ptc_js_wait_invoke(args: &Value) -> ToolResult {
    let env = process_environ();
    if !ptc_js_enabled(&env) {
        return ToolResult::fail("wait_js is disabled");
    }
    invoke_wait_js_live(args, &env).await
}

async fn run_code_invoke(args: &Value) -> ToolResult {
    let env = process_environ();
    if !run_code_enabled(&env) {
        return ToolResult::fail("run_code is disabled");
    }
    let code = args.get("code").and_then(Value::as_str).unwrap_or("");
    if let Some(error) = source_cap_error(code) {
        return error;
    }
    if code.trim().is_empty() {
        return ToolResult::fail("code is empty");
    }
    let description = args
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("run_code");
    invoke_run_code_child(
        code,
        description,
        &env,
        Box::new(|name, arguments| Box::pin(nested_run_code_tool(name, arguments))),
    )
    .await
}

async fn nested_run_code_tool(name: String, arguments: Map<String, Value>) -> ToolResult {
    match name.as_str() {
        TODO_TOOL_NAME => {
            let mut store = TODO_STORE.lock().expect("todo store");
            todo_write_result(&mut store, &Value::Object(arguments), "")
        }
        WEB_FETCH => {
            let url = arguments
                .get("url")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            web_fetch_live(&url, &web_config()).await
        }
        WEB_SEARCH => {
            let query = arguments.get("query").and_then(Value::as_str).unwrap_or("");
            let max_results = arguments.get("max_results").and_then(Value::as_u64);
            web_search_live(query, max_results, &web_config()).await
        }
        other => ToolResult::fail(format!("{other} is not implemented in rust sidecar yet")),
    }
}

async fn invoke_builtin(name: &str, args: &Value) -> ToolResult {
    match name {
        TODO_TOOL_NAME => {
            let mut store = TODO_STORE.lock().expect("todo store");
            todo_write_result(&mut store, args, "")
        }
        WEB_FETCH => {
            let url = args.get("url").and_then(Value::as_str).unwrap_or("");
            web_fetch_live(url, &web_config()).await
        }
        WEB_SEARCH => {
            let query = args.get("query").and_then(Value::as_str).unwrap_or("");
            let max_results = args.get("max_results").and_then(Value::as_u64);
            web_search_live(query, max_results, &web_config()).await
        }
        RUN_CODE => run_code_invoke(args).await,
        RUN_JS => ptc_js_run_invoke(args).await,
        WAIT_JS => ptc_js_wait_invoke(args).await,
        other => ToolResult::fail(format!("{other} is not implemented in rust sidecar yet")),
    }
}

fn builtin_executor() -> RouterToolExecutor {
    let mut router = ToolRouter::new();
    router.register(TODO_TOOL_NAME, |args| async move {
        let mut store = TODO_STORE.lock().expect("todo store");
        Ok(todo_write_result(&mut store, &args, ""))
    });
    router.register(WEB_FETCH, |args| async move {
        let url = args
            .get("url")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        Ok(web_fetch_live(&url, &web_config()).await)
    });
    router.register(WEB_SEARCH, |args| async move {
        let query = args
            .get("query")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let max_results = args.get("max_results").and_then(Value::as_u64);
        Ok(web_search_live(&query, max_results, &web_config()).await)
    });
    router.register(
        RUN_CODE,
        |args| async move { Ok(run_code_invoke(&args).await) },
    );
    router.register(
        RUN_JS,
        |args| async move { Ok(ptc_js_run_invoke(&args).await) },
    );
    router.register(WAIT_JS, |args| async move {
        Ok(ptc_js_wait_invoke(&args).await)
    });
    RouterToolExecutor::new(router)
}

pub async fn run_turn(messages: Vec<LLMMessage>, stream_id: String, llm: TurnLlm) -> Vec<Value> {
    let mut notifications = Vec::new();
    let mut stream_failed = false;
    let executor = builtin_executor();
    let emit = |event: LoopEvent| {
        if event.kind == "error" {
            stream_failed = true;
            notifications.push(rpc::notification(
                "stream.error",
                json!({
                    "streamId": stream_id,
                    "message": event.data.get("message").cloned().unwrap_or(json!("error"))
                }),
            ));
            return;
        }
        if let Some(params) = chunk_from_event(&stream_id, &event) {
            notifications.push(rpc::notification("stream.chunk", params));
        }
    };
    if fake_llm() {
        let provider = ScriptedProvider::new(vec![ScriptedTurn {
            content: fake_reply(),
            ..ScriptedTurn::default()
        }]);
        let mut core = CoreLoop::new(provider, executor);
        core.run_emitting(messages, emit).await;
    } else {
        match http_provider(&llm) {
            Ok(Some(provider)) => {
                let mut core = CoreLoop::new(HttpLoopProvider { inner: provider }, executor)
                    .with_tools(listed_tools());
                core.run_emitting(messages, emit).await;
            }
            Ok(None) => {
                let provider = ScriptedProvider::new(vec![ScriptedTurn {
                    content: "rust sidecar: no LLM configured (set STEERABLE_SIDECAR_FAKE_LLM=1 or STEERABLE_BASE_URL)".into(),
                    ..ScriptedTurn::default()
                }]);
                let mut core = CoreLoop::new(provider, executor);
                core.run_emitting(messages, emit).await;
            }
            Err(error) => {
                stream_failed = true;
                notifications.push(rpc::notification(
                    "stream.error",
                    json!({"streamId": stream_id, "message": error}),
                ));
            }
        }
    }
    notifications.push(rpc::notification(
        "stream.done",
        json!({"streamId": stream_id, "ok": !stream_failed, "engine": "rust"}),
    ));
    notifications
}

fn fake_llm() -> bool {
    matches!(
        std::env::var("STEERABLE_SIDECAR_FAKE_LLM")
            .unwrap_or_default()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn fake_reply() -> String {
    std::env::var("STEERABLE_SIDECAR_FAKE_REPLY").unwrap_or_else(|_| "ok".into())
}

fn env_nonempty(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn param_nonempty(map: &serde_json::Map<String, Value>, keys: &[&str]) -> Option<String> {
    for key in keys {
        if let Some(value) = map
            .get(*key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            return Some(value.to_string());
        }
    }
    None
}

fn turn_llm_from_params(map: &serde_json::Map<String, Value>) -> TurnLlm {
    TurnLlm {
        provider: param_nonempty(map, &["provider"])
            .or_else(|| env_nonempty("STEERABLE_PROVIDER"))
            .unwrap_or_else(|| "openai_compat".into()),
        base_url: param_nonempty(map, &["baseUrl", "base_url"])
            .or_else(|| env_nonempty("STEERABLE_BASE_URL")),
        api_key: param_nonempty(map, &["apiKey", "api_key"])
            .or_else(|| env_nonempty("STEERABLE_API_KEY")),
        model: param_nonempty(map, &["model"]).or_else(|| env_nonempty("STEERABLE_MODEL")),
        temperature: map.get("temperature").and_then(Value::as_f64),
        max_tokens: map
            .get("maxTokens")
            .or_else(|| map.get("max_tokens"))
            .and_then(Value::as_i64),
        reasoning_effort: param_nonempty(map, &["reasoningEffort", "reasoning_effort"]),
    }
}

fn http_provider(llm: &TurnLlm) -> Result<Option<HttpProvider>, String> {
    let Some(base_url) = llm.base_url.as_deref() else {
        return Ok(None);
    };
    let model = llm.model.clone().unwrap_or_else(|| "gpt-4o-mini".into());
    let provider = match llm.provider.trim().to_ascii_lowercase().as_str() {
        "openai" | "openai_compat" | "openai-compatible" => {
            let mut provider = OpenAICompatProvider::new("openai_compat", model, base_url);
            provider.api_key = llm.api_key.clone();
            provider.default_temperature = llm.temperature;
            provider.default_max_tokens = llm.max_tokens;
            provider.reasoning_effort = llm.reasoning_effort.clone();
            HttpProvider::OpenAI(provider)
        }
        "ollama" => {
            let base_url = if base_url.trim_end_matches('/').ends_with("/v1") {
                base_url.to_string()
            } else {
                format!("{}/v1", base_url.trim_end_matches('/'))
            };
            let mut provider = OpenAICompatProvider::new("ollama", model, base_url);
            provider.api_key = llm.api_key.clone();
            provider.default_temperature = llm.temperature;
            provider.default_max_tokens = llm.max_tokens;
            provider.reasoning_effort = llm.reasoning_effort.clone();
            HttpProvider::OpenAI(provider)
        }
        "responses" | "openai-responses" | "openai_responses" | "xai" => {
            let mut provider = OpenAIResponsesProvider::new("openai_responses", model, base_url);
            provider.api_key = llm.api_key.clone();
            provider.default_temperature = llm.temperature;
            provider.default_max_tokens = llm.max_tokens;
            provider.reasoning_effort = llm.reasoning_effort.clone();
            HttpProvider::Responses(provider)
        }
        "anthropic" | "claude" => {
            let mut provider = AnthropicProvider::new("anthropic", model, base_url);
            provider.api_key = llm.api_key.clone();
            provider.default_temperature = llm.temperature;
            if let Some(max_tokens) = llm.max_tokens {
                provider.default_max_tokens = max_tokens;
            }
            HttpProvider::Anthropic(provider)
        }
        "google" | "gemini" | "google-genai" | "google_genai" => {
            let mut provider = GoogleGenAIProvider::new("google_genai", model, base_url);
            provider.api_key = llm.api_key.clone();
            provider.default_temperature = llm.temperature;
            provider.default_max_tokens = llm.max_tokens;
            HttpProvider::Gemini(provider)
        }
        other => return Err(format!("unknown provider: {other:?}")),
    };
    Ok(Some(provider))
}

enum HttpProvider {
    OpenAI(OpenAICompatProvider),
    Responses(OpenAIResponsesProvider),
    Anthropic(AnthropicProvider),
    Gemini(GoogleGenAIProvider),
}

struct HttpLoopProvider {
    inner: HttpProvider,
}

#[async_trait::async_trait]
impl steerable_agent_runtime::LLMProvider for HttpLoopProvider {
    fn name(&self) -> &str {
        match &self.inner {
            HttpProvider::OpenAI(provider) => &provider.name,
            HttpProvider::Responses(provider) => &provider.name,
            HttpProvider::Anthropic(provider) => &provider.name,
            HttpProvider::Gemini(provider) => &provider.name,
        }
    }

    fn model(&self) -> &str {
        match &self.inner {
            HttpProvider::OpenAI(provider) => &provider.model,
            HttpProvider::Responses(provider) => &provider.model,
            HttpProvider::Anthropic(provider) => &provider.model,
            HttpProvider::Gemini(provider) => &provider.model,
        }
    }

    async fn stream(
        &mut self,
        messages: &[LLMMessage],
    ) -> Result<Vec<steerable_agent_runtime::LLMStreamChunk>, steerable_agent_runtime::LLMError>
    {
        self.stream_with_tools(messages, &[]).await
    }

    async fn stream_with_tools(
        &mut self,
        messages: &[LLMMessage],
        tools: &[Value],
    ) -> Result<Vec<steerable_agent_runtime::LLMStreamChunk>, steerable_agent_runtime::LLMError>
    {
        match &self.inner {
            HttpProvider::OpenAI(provider) => {
                provider.stream(messages, Some(tools), &Map::new()).await
            }
            HttpProvider::Responses(provider) => {
                provider.stream(messages, Some(tools), &Map::new()).await
            }
            HttpProvider::Anthropic(provider) => {
                provider.stream(messages, Some(tools), &Map::new()).await
            }
            HttpProvider::Gemini(provider) => {
                provider.stream(messages, Some(tools), &Map::new()).await
            }
        }
    }
}

#[cfg(test)]
#[path = "methods_tests.rs"]
mod tests;
