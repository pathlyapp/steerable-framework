use std::sync::Mutex;

use pretty_assertions::assert_eq;
use serde_json::{json, Map, Value};
use steerable_agent_runtime::{
    catalog_provider_for_base_url, classify_http_status, compat_for_base_url, consume_sse_lines,
    decode_tool_calls, describe_catalog_providers, encode_gemini_contents, encode_message,
    encode_responses_input, estimate_cost_usd, estimate_text_tokens, estimate_tokens,
    openai_tool_to_anthropic, parse_anthropic_event, parse_gemini_chunk, parse_responses_event,
    parse_retry_after_ms, parse_stream_chunk, resolve_context_window, resolve_in_catalog,
    resolve_leaf_cross_provider, responses_tool, sanitize_tool_name, split_system_and_messages,
    stream_timeout, AnthropicProvider, GoogleGenAIProvider, LLMErrorKind, LLMMessage,
    OpenAICompatFlags, OpenAICompatProvider, OpenAIResponsesProvider, OpenAIToolCallAssembler,
    ResponsesToolCallAssembler, ToolCall,
};

static ENV_LOCK: Mutex<()> = Mutex::new(());

fn map(pairs: &[(&str, Value)]) -> Map<String, Value> {
    pairs
        .iter()
        .map(|(k, v)| ((*k).to_string(), v.clone()))
        .collect()
}

#[test]
fn encode_message_simple_text() {
    let encoded = encode_message(
        &LLMMessage::text("user", "hi"),
        &OpenAICompatFlags::default(),
    );
    assert_eq!(encoded, json!({"role": "user", "content": "hi"}));
}

#[test]
fn encode_message_with_tool_calls() {
    let mut msg = LLMMessage::text("assistant", "");
    msg.tool_calls = vec![ToolCall {
        id: "call_1".into(),
        name: "web_search".into(),
        arguments: json!({"limit": 5}),
    }];
    let encoded = encode_message(&msg, &OpenAICompatFlags::default());
    assert_eq!(encoded["role"], json!("assistant"));
    assert_eq!(encoded["tool_calls"][0]["id"], json!("call_1"));
    assert_eq!(encoded["tool_calls"][0]["type"], json!("function"));
    assert_eq!(
        encoded["tool_calls"][0]["function"]["name"],
        json!("web_search")
    );
    let args: Value = serde_json::from_str(
        encoded["tool_calls"][0]["function"]["arguments"]
            .as_str()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(args, json!({"limit": 5}));
}

#[test]
fn encode_message_tool_response() {
    let mut msg = LLMMessage::text("tool", "ok");
    msg.tool_call_id = Some("call_1".into());
    msg.name = Some("web_search".into());
    let encoded = encode_message(&msg, &OpenAICompatFlags::default());
    assert_eq!(
        encoded,
        json!({
            "role": "tool",
            "content": "ok",
            "name": "web_search",
            "tool_call_id": "call_1",
        })
    );
}

#[test]
fn decode_tool_calls_round_trip() {
    let raw = json!([{
        "id": "c1",
        "type": "function",
        "function": {"name": "search", "arguments": "{\"q\":\"x\"}"},
    }]);
    let decoded = decode_tool_calls(&raw).unwrap();
    assert_eq!(decoded.len(), 1);
    assert_eq!(decoded[0].id, "c1");
    assert_eq!(decoded[0].name, "search");
    assert_eq!(decoded[0].arguments, json!({"q": "x"}));
}

#[test]
fn decode_tool_calls_invalid_arguments() {
    let raw = json!([{
        "id": "c1",
        "type": "function",
        "function": {"name": "search", "arguments": "{"},
    }]);
    let decoded = decode_tool_calls(&raw).unwrap();
    assert_eq!(decoded[0].arguments, json!({}));
}

#[test]
fn parse_stream_chunk_text_delta() {
    let chunk = json!({
        "choices": [{"delta": {"content": "hello"}, "finish_reason": null}],
    });
    let parsed = parse_stream_chunk(&chunk, &OpenAICompatFlags::default()).unwrap();
    assert_eq!(parsed.content_delta.as_deref(), Some("hello"));
    assert!(parsed.tool_call_delta.is_none());
}

#[test]
fn parse_stream_chunk_reasoning_delta() {
    let chunk = json!({
        "choices": [{"delta": {"reasoning_content": "thinking…"}, "finish_reason": null}],
    });
    let parsed = parse_stream_chunk(&chunk, &OpenAICompatFlags::default()).unwrap();
    assert_eq!(parsed.reasoning_delta.as_deref(), Some("thinking…"));
    assert!(parsed.content_delta.is_none());
}

#[test]
fn parse_stream_chunk_reasoning_details_preserved() {
    let details = json!([{"type": "reasoning.text", "text": "plan"}]);
    let chunk = json!({
        "choices": [{"delta": {
            "reasoning": "plan",
            "reasoning_details": details,
        }, "finish_reason": null}],
    });
    let parsed = parse_stream_chunk(&chunk, &OpenAICompatFlags::default()).unwrap();
    assert_eq!(parsed.reasoning_delta.as_deref(), Some("plan"));
    assert_eq!(parsed.reasoning_details, Some(details));
}

#[test]
fn parse_stream_chunk_usage_only() {
    let chunk = json!({
        "choices": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    });
    let parsed = parse_stream_chunk(&chunk, &OpenAICompatFlags::default()).unwrap();
    let usage = parsed.usage.unwrap();
    assert_eq!(usage.prompt_tokens, 10);
    assert_eq!(usage.completion_tokens, 4);
    assert_eq!(usage.total_tokens, 14);
    assert!(parsed.content_delta.is_none());
}

#[test]
fn parse_stream_chunk_usage_on_final_choices_chunk() {
    let chunk = json!({
        "choices": [{"delta": {"content": ""}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 4},
        },
    });
    let parsed = parse_stream_chunk(&chunk, &OpenAICompatFlags::default()).unwrap();
    let usage = parsed.usage.unwrap();
    assert_eq!(usage.prompt_tokens, 12);
    assert_eq!(usage.completion_tokens, 3);
    assert_eq!(usage.cached_prompt_tokens, 4);
    assert_eq!(parsed.finish_reason.as_deref(), Some("stop"));
}

#[test]
fn parse_stream_chunk_tool_call_argument_fragments_are_not_json() {
    let chunk = json!({
        "choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call_1",
            "function": {"name": "search", "arguments": "{"},
        }]}, "finish_reason": null}],
    });
    let parsed = parse_stream_chunk(&chunk, &OpenAICompatFlags::default()).unwrap();
    assert_eq!(parsed.tool_call_delta.as_ref().unwrap().name, "search");
}

#[test]
fn parse_stream_chunk_tool_call_delta() {
    let chunk = json!({
        "choices": [{"delta": {"tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {"name": "search", "arguments": "{}"},
        }]}, "finish_reason": "tool_calls"}],
    });
    let parsed = parse_stream_chunk(&chunk, &OpenAICompatFlags::default()).unwrap();
    let tool = parsed.tool_call_delta.unwrap();
    assert_eq!(tool.id, "c1");
    assert_eq!(tool.name, "search");
    assert_eq!(parsed.finish_reason.as_deref(), Some("tool_calls"));
}

#[test]
fn encode_message_echoes_reasoning_details() {
    let mut msg = LLMMessage::text("assistant", "ok");
    msg.reasoning_details =
        Some(json!([{"id": "rs_1", "type": "reasoning.summary", "summary": "plan"}]));
    let encoded = encode_message(&msg, &OpenAICompatFlags::default());
    assert_eq!(
        encoded["reasoning_details"],
        json!([{"id": "rs_1", "type": "reasoning.summary", "summary": "plan"}])
    );
    assert!(encoded.get("reasoning").is_none());
}

#[test]
fn encode_message_falls_back_to_plaintext_reasoning() {
    let mut msg = LLMMessage::text("assistant", "ok");
    msg.reasoning = Some("plan".into());
    let encoded = encode_message(&msg, &OpenAICompatFlags::default());
    assert_eq!(encoded["reasoning"], json!("plan"));
    assert!(encoded.get("reasoning_details").is_none());
}

#[test]
fn encode_message_echo_field_is_compat_data() {
    let mut msg = LLMMessage::text("assistant", "ok");
    msg.reasoning = Some("plan".into());
    let deepseek = compat_for_base_url("https://api.deepseek.com/v1").unwrap();
    let encoded = encode_message(&msg, &deepseek);
    assert_eq!(encoded["reasoning_content"], json!("plan"));
    assert!(encoded.get("reasoning").is_none());
}

#[test]
fn encode_message_echoes_empty_reasoning_for_deepseek_tool_calls() {
    let mut msg = LLMMessage::text("assistant", "");
    msg.tool_calls = vec![ToolCall {
        id: "call_1".into(),
        name: "search".into(),
        arguments: json!({"q": "x"}),
    }];
    let deepseek = compat_for_base_url("https://api.deepseek.com/v1").unwrap();
    let encoded = encode_message(&msg, &deepseek);
    assert_eq!(encoded["reasoning_content"], json!(""));
    let reference = encode_message(&msg, &OpenAICompatFlags::default());
    assert!(reference.get("reasoning").is_none());
    assert!(reference.get("reasoning_content").is_none());
}

#[test]
fn assembler_concatenates_argument_fragments() {
    let mut assembler = OpenAIToolCallAssembler::default();
    assembler.observe(&json!({
        "choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call_1",
            "function": {"name": "search", "arguments": "{"},
        }]}}],
    }));
    assembler.observe(&json!({
        "choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "function": {"arguments": "\"q\":\"hi\"}"},
        }]}}],
    }));
    let calls = assembler.flush();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].id, "call_1");
    assert_eq!(calls[0].name, "search");
    assert_eq!(calls[0].arguments, json!({"q": "hi"}));
}

#[test]
fn assembler_keeps_parallel_tool_calls_by_index() {
    let mut assembler = OpenAIToolCallAssembler::default();
    assembler.observe(&json!({
        "choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c0", "function": {"name": "a", "arguments": "{\"x\":1}"}},
            {"index": 1, "id": "c1", "function": {"name": "b", "arguments": "{\"y\":2}"}},
        ]}}],
    }));
    let calls = assembler.flush();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0].name, "a");
    assert_eq!(calls[1].name, "b");
    assert_eq!(calls[0].arguments, json!({"x": 1}));
    assert_eq!(calls[1].arguments, json!({"y": 2}));
}

#[test]
fn sanitize_harmony_leaked_tool_names() {
    assert_eq!(
        sanitize_tool_name("to=functions.exec_command<|channel|>commentary"),
        "exec_command"
    );
    assert_eq!(
        sanitize_tool_name("functions.exec_command<|channel|>commentary"),
        "exec_command"
    );
    assert_eq!(
        sanitize_tool_name("exec_command<|channel|>commentary"),
        "exec_command"
    );
    assert_eq!(sanitize_tool_name("json<|constrain|>json"), "json");
    assert_eq!(sanitize_tool_name("exec_command"), "exec_command");
}

#[test]
fn decode_and_parse_sanitize_harmony_names() {
    let decoded = decode_tool_calls(&json!([{
        "id": "c1",
        "function": {
            "name": "to=functions.search<|channel|>commentary",
            "arguments": "{}",
        },
    }]))
    .unwrap();
    assert_eq!(decoded[0].name, "search");
    let parsed = parse_stream_chunk(
        &json!({
            "choices": [{"delta": {"tool_calls": [{
                "id": "c1",
                "function": {"name": "search<|channel|>commentary", "arguments": "{}"},
            }]}}],
        }),
        &OpenAICompatFlags::default(),
    )
    .unwrap();
    assert_eq!(parsed.tool_call_delta.unwrap().name, "search");
}

#[test]
fn openai_compat_stream_options_on_stream_only() {
    let provider = OpenAICompatProvider::new("local", "test-model", "http://localhost:9/v1");
    let extra = Map::new();
    let stream_body = provider.build_body(
        &[LLMMessage::text("user", "hi")],
        None,
        None,
        None,
        true,
        &extra,
    );
    let complete_body = provider.build_body(
        &[LLMMessage::text("user", "hi")],
        None,
        None,
        None,
        false,
        &extra,
    );
    assert_eq!(
        stream_body["stream_options"],
        json!({"include_usage": true})
    );
    assert!(complete_body.get("stream_options").is_none());
}

#[test]
fn openai_compat_http_error_classifies_rate_limit() {
    assert_eq!(
        classify_http_status(429, "rate limited"),
        LLMErrorKind::RateLimit
    );
    assert_eq!(parse_retry_after_ms("12"), Some(12_000));
}

#[test]
fn openai_compat_stream_timeouts_follow_env() {
    let _guard = ENV_LOCK.lock().unwrap();
    std::env::set_var("STEERABLE_LLM_CONNECT_TIMEOUT_SEC", "12");
    std::env::set_var("STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC", "90");
    let (connect, read) = stream_timeout();
    std::env::remove_var("STEERABLE_LLM_CONNECT_TIMEOUT_SEC");
    std::env::remove_var("STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC");
    assert_eq!(connect, 12.0);
    assert_eq!(read, 90.0);
}

#[test]
fn openai_compat_openrouter_provider_pin() {
    let _guard = ENV_LOCK.lock().unwrap();
    std::env::set_var("STEERABLE_OPENROUTER_PROVIDER", "Z.ai");
    std::env::set_var("STEERABLE_OPENROUTER_ALLOW_FALLBACKS", "0");
    std::env::set_var("STEERABLE_OPENROUTER_REQUIRE_PARAMETERS", "0");
    let provider = OpenAICompatProvider::new("or", "z-ai/glm-5", "https://openrouter.ai/api/v1");
    let body = provider.build_body(
        &[LLMMessage::text("user", "hi")],
        None,
        None,
        None,
        false,
        &Map::new(),
    );
    std::env::remove_var("STEERABLE_OPENROUTER_PROVIDER");
    std::env::remove_var("STEERABLE_OPENROUTER_ALLOW_FALLBACKS");
    std::env::remove_var("STEERABLE_OPENROUTER_REQUIRE_PARAMETERS");
    assert_eq!(
        body["provider"],
        json!({
            "order": ["Z.ai"],
            "only": ["Z.ai"],
            "allow_fallbacks": false,
            "require_parameters": false,
        })
    );
}

#[test]
fn openai_compat_coerces_required_tool_choice_for_z_ai() {
    let glm = OpenAICompatProvider::new("or", "z-ai/glm-5", "https://openrouter.ai/api/v1");
    let extra = map(&[("tool_choice", json!("required"))]);
    let body = glm.build_body(
        &[LLMMessage::text("user", "hi")],
        None,
        None,
        None,
        false,
        &extra,
    );
    assert_eq!(body["tool_choice"], json!("auto"));

    let ds = OpenAICompatProvider::new("ds", "deepseek-chat", "https://api.deepseek.com");
    let body = ds.build_body(
        &[LLMMessage::text("user", "hi")],
        None,
        None,
        None,
        false,
        &extra,
    );
    assert_eq!(body["tool_choice"], json!("auto"));
}

#[test]
fn openai_compat_coerces_required_tool_choice_for_qwen_thinking() {
    let qwen = OpenAICompatProvider::new(
        "or",
        "qwen/qwen3-coder-next",
        "https://openrouter.ai/api/v1",
    );
    let extra = map(&[("tool_choice", json!("required"))]);
    let body = qwen.build_body(
        &[LLMMessage::text("user", "hi")],
        None,
        None,
        None,
        false,
        &extra,
    );
    assert_eq!(body["tool_choice"], json!("auto"));
}

#[test]
fn openai_compat_coerces_required_tool_choice_for_openrouter_deepseek() {
    let ds = OpenAICompatProvider::new(
        "or",
        "deepseek/deepseek-v3.2",
        "https://openrouter.ai/api/v1",
    );
    let extra = map(&[("tool_choice", json!("required"))]);
    let body = ds.build_body(
        &[LLMMessage::text("user", "hi")],
        None,
        None,
        None,
        false,
        &extra,
    );
    assert_eq!(body["tool_choice"], json!("auto"));
}

#[test]
fn anthropic_split_system_and_tool_response() {
    let (system, formatted) = split_system_and_messages(&[
        LLMMessage::text("system", "be brief"),
        LLMMessage::text("user", "hi"),
    ]);
    assert_eq!(system.as_deref(), Some("be brief"));
    assert_eq!(formatted, vec![json!({"role": "user", "content": "hi"})]);

    let mut tool_msg = LLMMessage::text("tool", "ok");
    tool_msg.tool_call_id = Some("t1".into());
    let (_system, formatted) = split_system_and_messages(&[tool_msg]);
    assert_eq!(
        formatted,
        vec![json!({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        })]
    );
}

#[test]
fn anthropic_assistant_tool_use_blocks() {
    let mut msg = LLMMessage::text("assistant", "");
    msg.tool_calls = vec![ToolCall {
        id: "t1".into(),
        name: "search".into(),
        arguments: json!({"q": "x"}),
    }];
    let (_system, formatted) = split_system_and_messages(&[msg]);
    assert_eq!(
        formatted,
        vec![json!({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "x"}}],
        })]
    );
}

#[test]
fn openai_tool_to_anthropic_shape() {
    let converted = openai_tool_to_anthropic(&json!({
        "type": "function",
        "function": {
            "name": "search",
            "description": "look up",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }));
    assert_eq!(converted["name"], json!("search"));
    assert_eq!(converted["description"], json!("look up"));
    assert_eq!(
        converted["input_schema"]["properties"]["q"]["type"],
        json!("string")
    );
}

#[test]
fn openai_tool_to_anthropic_preserves_cache_control() {
    let converted = openai_tool_to_anthropic(&json!({
        "type": "function",
        "function": {
            "name": "search",
            "description": "",
            "parameters": {"type": "object"},
        },
        "cache_control": {"type": "ephemeral"},
    }));
    assert_eq!(converted["cache_control"], json!({"type": "ephemeral"}));
}

#[test]
fn responses_wire_encodes_items_and_events() {
    let mut assistant = LLMMessage::text("assistant", "checking");
    assistant.tool_calls = vec![ToolCall::new("exec", json!({"cmd": "ls"})).with_id("c1")];
    let mut tool = LLMMessage::text("tool", "file.txt");
    tool.tool_call_id = Some("c1".into());
    let (instructions, input) =
        encode_responses_input(&[LLMMessage::text("system", "be terse"), assistant, tool]);
    assert_eq!(instructions, "be terse");
    assert_eq!(input[1]["type"], "function_call");
    assert_eq!(input[2]["type"], "function_call_output");
    let provider = OpenAIResponsesProvider::new("openai", "gpt-5", "https://api.openai.com/v1");
    let body = provider.build_body(
        &[LLMMessage::text("user", "hi")],
        None,
        None,
        Some(512),
        true,
        &Map::new(),
    );
    assert_eq!(body["max_output_tokens"], 512);
    assert_eq!(body["store"], false);
    let chunk = parse_responses_event(&json!({
        "type": "response.completed",
        "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
    }))
    .unwrap();
    assert_eq!(chunk.finish_reason.as_deref(), Some("length"));
    assert_eq!(
        responses_tool(
            &json!({"type":"function","function":{"name":"exec","parameters":{"type":"object"}}})
        )["name"],
        "exec"
    );
}

#[test]
fn responses_tool_call_assembler_joins_arguments() {
    let mut assembler = ResponsesToolCallAssembler::default();
    assembler.observe(&json!({
        "type":"response.output_item.added",
        "output_index":0,
        "item":{"type":"function_call","call_id":"c1","name":"exec"}
    }));
    assembler.observe(&json!({
        "type":"response.function_call_arguments.delta",
        "output_index":0,
        "delta":"{\"cmd\":\"echo hi\"}"
    }));
    let calls = assembler.flush();
    assert_eq!(calls[0].arguments, json!({"cmd":"echo hi"}));
}

#[test]
fn gemini_wire_encodes_and_decodes_native_parts() {
    let mut assistant = LLMMessage::text("assistant", "checking");
    assistant.tool_calls = vec![ToolCall::new("exec", json!({"cmd": "ls"}))];
    let (system, contents) =
        encode_gemini_contents(&[LLMMessage::text("system", "brief"), assistant]);
    assert_eq!(system, "brief");
    assert_eq!(contents[0]["role"], "model");
    let provider = GoogleGenAIProvider::new(
        "google",
        "gemini-3-pro",
        "https://generativelanguage.googleapis.com",
    );
    let body = provider.build_body(
        &[LLMMessage::text("user", "hi")],
        None,
        Some(0.4),
        Some(512),
        &Map::new(),
    );
    assert_eq!(body["generationConfig"]["maxOutputTokens"], 512);
    let chunks = parse_gemini_chunk(&json!({
        "candidates":[{"content":{"parts":[{"functionCall":{"name":"exec","args":{"cmd":"ls"}}}]},"finishReason":"STOP"}],
        "usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":2,"totalTokenCount":5}
    }));
    assert_eq!(
        chunks
            .iter()
            .find_map(|chunk| chunk.tool_call_delta.as_ref())
            .unwrap()
            .name,
        "exec"
    );
    assert_eq!(
        chunks
            .iter()
            .find_map(|chunk| chunk.usage.as_ref())
            .unwrap()
            .total_tokens,
        5
    );
}

#[test]
fn anthropic_provider_builds_native_body_and_parses_delta() {
    let provider = AnthropicProvider::new("anthropic", "claude", "https://api.anthropic.com");
    let body = provider.build_body(
        &[
            LLMMessage::text("system", "brief"),
            LLMMessage::text("user", "hi"),
        ],
        None,
        None,
        Some(1024),
        &Map::new(),
    );
    assert_eq!(body["system"], "brief");
    assert_eq!(body["max_tokens"], 1024);
    let chunk = parse_anthropic_event(&json!({
        "type":"content_block_delta",
        "delta":{"type":"text_delta","text":"hello"}
    }))
    .unwrap();
    assert_eq!(chunk.content_delta.as_deref(), Some("hello"));
}

#[test]
fn provider_presets_apply_as_fill_only_defaults() {
    let qwen = OpenAICompatProvider::new(
        "openrouter",
        "qwen/qwen3-32b",
        "https://openrouter.ai/api/v1",
    );
    let body = qwen.build_body(
        &[LLMMessage::text("user", "hi")],
        None,
        None,
        None,
        false,
        &Map::new(),
    );
    assert_eq!(body["temperature"], 0.6);
    assert_eq!(body["top_p"], 0.95);
    assert_eq!(body["top_k"], 20);

    let explicit = qwen.build_body(
        &[LLMMessage::text("user", "hi")],
        None,
        Some(0.2),
        None,
        false,
        &map(&[("top_p", json!(0.4))]),
    );
    assert_eq!(explicit["temperature"], 0.2);
    assert_eq!(explicit["top_p"], 0.4);
}

#[test]
fn generated_catalog_resolves_models_and_provider_endpoints() {
    let exact = resolve_in_catalog(Some("openai"), "gpt-5.5").unwrap();
    assert_eq!(exact.source, "exact");
    assert!(exact.context_window > 0);
    let leaf = resolve_leaf_cross_provider("gateway/z-ai/glm-5.3-flash").unwrap();
    assert_eq!(leaf.source, "leaf");
    assert_eq!(
        catalog_provider_for_base_url("https://openrouter.ai/api/v1").as_deref(),
        Some("openrouter")
    );
    let providers = describe_catalog_providers();
    let openai = providers
        .iter()
        .find(|provider| provider["id"] == "openai")
        .unwrap();
    assert_eq!(openai["wireKind"], "openai_compat");
    assert!(openai["models"]
        .as_array()
        .is_some_and(|models| !models.is_empty()));
}

#[test]
fn pricing_and_token_estimation_match_runtime_basics() {
    assert_eq!(estimate_text_tokens("abcd"), 1);
    assert_eq!(estimate_text_tokens("中文"), 2);
    assert_eq!(
        estimate_cost_usd("gpt-5", 1_000_000, 1_000_000),
        Some(11.25)
    );
    assert_eq!(estimate_cost_usd("local-model", 100, 100), None);
    assert!(estimate_tokens(&[LLMMessage::text("user", "hello")], "unknown") >= 10);
    assert!(resolve_context_window("gpt-5.5", None, Some("openai")) > 0);
}

#[test]
fn compat_for_deepseek_and_openrouter() {
    let deepseek = compat_for_base_url("https://api.deepseek.com/v1").unwrap();
    assert_eq!(deepseek.reasoning_echo_field, "reasoning_content");
    assert!(!deepseek.supports_forced_tool_choice);
    assert!(deepseek.echo_empty_reasoning_for_tool_calls);
    let openrouter = compat_for_base_url("https://openrouter.ai/api/v1").unwrap();
    assert_eq!(
        openrouter.reasoning_delta_fields,
        &["reasoning", "reasoning_content"]
    );
}

#[test]
fn consume_sse_lines_emits_text_then_assembled_tool_call() {
    let lines = [
        r#"data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}"#,
        r#"data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"search","arguments":"{"}}]},"finish_reason":null}]}"#,
        r#"data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"q\":\"x\"}"}}]},"finish_reason":"tool_calls"}]}"#,
        "data: [DONE]",
    ];
    let chunks = consume_sse_lines(lines, &OpenAICompatFlags::default());
    assert_eq!(chunks[0].content_delta.as_deref(), Some("hi"));
    let tool = chunks
        .iter()
        .rev()
        .find_map(|chunk| chunk.tool_call_delta.as_ref())
        .unwrap();
    assert_eq!(tool.name, "search");
    assert_eq!(tool.arguments, json!({"q": "x"}));
}

#[tokio::test]
async fn openai_compat_http_stream_reads_sse() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut buf = vec![0u8; 8192];
        let _ = socket.read(&mut buf).await;
        let sse = concat!(
            r#"data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}"#,
            "\n\n",
            "data: [DONE]\n\n",
        );
        let resp = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{sse}",
            sse.len()
        );
        let _ = socket.write_all(resp.as_bytes()).await;
    });
    let provider = OpenAICompatProvider::new("local", "test-model", format!("http://{addr}/v1"));
    let chunks = provider
        .stream(&[LLMMessage::text("user", "hi")], None, &Map::new())
        .await
        .unwrap();
    assert_eq!(chunks[0].content_delta.as_deref(), Some("hello"));
    assert_eq!(chunks[0].finish_reason.as_deref(), Some("stop"));
}

async fn serve_sse_once(sse: String) -> std::net::SocketAddr {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut buf = vec![0u8; 16_384];
        let _ = socket.read(&mut buf).await;
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{sse}",
            sse.len()
        );
        socket.write_all(response.as_bytes()).await.unwrap();
    });
    addr
}

#[tokio::test]
async fn responses_http_stream_reads_typed_sse() {
    let sse = concat!(
        "data: {\"type\":\"response.output_text.delta\",\"delta\":\"hello\"}\n\n",
        "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\",\"usage\":{\"input_tokens\":2,\"output_tokens\":1,\"total_tokens\":3}}}\n\n",
        "data: [DONE]\n\n"
    );
    let addr = serve_sse_once(sse.into()).await;
    let provider = OpenAIResponsesProvider::new("responses", "gpt-5", format!("http://{addr}/v1"));
    let chunks = provider
        .stream(&[LLMMessage::text("user", "hi")], None, &Map::new())
        .await
        .unwrap();
    assert_eq!(chunks[0].content_delta.as_deref(), Some("hello"));
    assert_eq!(chunks[1].usage.as_ref().unwrap().total_tokens, 3);
}

#[tokio::test]
async fn gemini_http_stream_reads_native_sse() {
    let sse = concat!(
        "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"hello\"}]},\"finishReason\":\"STOP\"}],\"usageMetadata\":{\"promptTokenCount\":2,\"candidatesTokenCount\":1,\"totalTokenCount\":3}}\n\n",
        "data: [DONE]\n\n"
    );
    let addr = serve_sse_once(sse.into()).await;
    let provider = GoogleGenAIProvider::new("gemini", "gemini-test", format!("http://{addr}"));
    let chunks = provider
        .stream(&[LLMMessage::text("user", "hi")], None, &Map::new())
        .await
        .unwrap();
    assert_eq!(chunks[0].content_delta.as_deref(), Some("hello"));
    assert_eq!(
        chunks
            .iter()
            .find_map(|chunk| chunk.usage.as_ref())
            .unwrap()
            .total_tokens,
        3
    );
}

#[tokio::test]
async fn anthropic_http_stream_assembles_tool_call() {
    let sse = concat!(
        "event: content_block_start\n",
        "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"c1\",\"name\":\"exec\",\"input\":{}}}\n\n",
        "event: content_block_delta\n",
        "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"cmd\\\":\\\"ls\\\"}\"}}\n\n",
        "event: content_block_stop\n",
        "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n",
        "event: message_delta\n",
        "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\"},\"usage\":{\"output_tokens\":1}}\n\n"
    );
    let addr = serve_sse_once(sse.into()).await;
    let provider = AnthropicProvider::new("anthropic", "claude-test", format!("http://{addr}"));
    let chunks = provider
        .stream(&[LLMMessage::text("user", "hi")], None, &Map::new())
        .await
        .unwrap();
    let call = chunks
        .iter()
        .find_map(|chunk| chunk.tool_call_delta.as_ref())
        .unwrap();
    assert_eq!(call.name, "exec");
    assert_eq!(call.arguments, json!({"cmd":"ls"}));
}
