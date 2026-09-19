//! Steerable CoreLoop (Rust).
//!
//! Ports `packages/agent-runtime/py` `CoreLoop.run`: think → act → observe,
//! yielding the same `LoopEvent` kinds the sidecar and API already consume.

mod anthropic_http;
mod anthropic_wire;
mod budget;
mod compat;
mod engine;
mod errors;
mod executor;
mod gemini_http;
mod gemini_wire;
mod hooks;
mod json_py;
mod model_resolve;
mod openai_http;
mod openai_wire;
mod presets;
mod pricing;
mod provider;
mod ptc_js;
mod responses_http;
mod responses_wire;
mod run_code;
mod skills;
mod todo;
mod tokens;
mod types;
mod web;
mod web_html;
mod web_search;
#[cfg(feature = "python")]
mod wire;
mod write_lease;

#[cfg(feature = "python")]
mod python;

pub use anthropic_wire::{
    openai_tool_to_anthropic, parse_anthropic_event, parse_anthropic_usage,
    split_system_and_messages, AnthropicProvider,
};
pub use budget::{consume_budget, BudgetLimit, BudgetState, DEFAULT_CACHED_TOKEN_WEIGHT};
pub use compat::{compat_for_base_url, describe_compat_flags, OpenAICompatFlags};
pub use engine::{CoreLoop, LoopConfig, RoundControl};
pub use errors::{classify_http_status, parse_retry_after_ms, LLMError, LLMErrorKind};
pub use executor::{RouterToolExecutor, ToolExecutor, ToolRouter};
pub use gemini_wire::{
    encode_gemini_contents, parse_gemini_chunk, parse_gemini_usage, GoogleGenAIProvider,
};
pub use hooks::{
    CompletionDraft, HookAction, LoopHooks, NoopHooks, PreStepOutcome, RequestErrorAction,
};
pub use model_resolve::{
    catalog_provider_for_base_url, describe_catalog_providers, resolve_in_catalog,
    resolve_leaf_cross_provider, CatalogHit,
};
pub use openai_wire::{
    consume_sse_lines, decode_tool_calls, encode_message, parse_stream_chunk, sanitize_tool_name,
    stream_timeout, OpenAICompatProvider, OpenAIToolCallAssembler,
};
pub use presets::{
    describe_provider_presets, preset_for, provider_presets, PresetEntry, ProviderPreset,
};
pub use pricing::{estimate_cost_usd, price_for_model, ModelPrice, MODEL_PRICES};
pub use provider::{LLMProvider, LLMStreamChunk, LLMUsage, ScriptedProvider, ScriptedTurn};
pub use ptc_js::{
    invoke_run_js, invoke_wait_js, nested_ptc_refused, node_executable, node_unavailable,
    ptc_js_enabled, ptc_sandbox_unavailable, run_js_tool_descriptor, wait_js_tool_descriptor,
    worker_environ, DEFAULT_MAX_OUTPUT_CHARS, DEFAULT_YIELD_MS, RUN_JS, WAIT_JS,
};
pub use responses_wire::{
    encode_responses_input, parse_responses_event, parse_responses_usage, responses_tool,
    OpenAIResponsesProvider, ResponsesToolCallAssembler,
};
pub use run_code::{
    child_environ, drive_scripted, refuse_nested, run_code_enabled, run_code_tool_descriptor,
    sandbox_unavailable, sidecar_confined, source_cap_error, timeout_ms, PumpAction, RunCodePump,
    MAX_NESTED_CALLS, MAX_SOURCE_BYTES, RUN_CODE, RUN_CODE_DESCRIPTION,
};
pub use skills::{
    list_skills_rpc, render_skill_catalog, select_catalog, select_skills, FilesystemSkillProvider,
    SkillDefinition, DEFAULT_MAX_CATALOG_SKILLS, EAGER_PRIORITY_THRESHOLD,
};
pub use todo::{
    apply_todo_write, todo_write_result, todo_write_tool_descriptor, TodoStore, TODO_DESCRIPTION,
    TODO_TOOL_NAME,
};
pub use tokens::{
    estimate_text_tokens, estimate_tokens, factor_for_model, resolve_context_window,
    IMAGE_PART_TOKEN_ESTIMATE,
};
pub use types::{
    ContentPart, LLMMessage, LoopContext, LoopEvent, ToolCall, ToolResult, MAX_COMPLETION_REDOS,
    MAX_READ_IMAGES_PER_ROUND,
};
pub use web::{
    assert_public_address, clamp_search_results, domain_policy_error, html_to_text,
    parse_fetch_url, same_origin, tool_descriptor, validate_fetch_target, web_fetch,
    web_fetch_live, web_fetch_schema, web_search_hits, web_search_schema, HttpResponse,
    WebSearchHit, WebToolsConfig, WEB_FETCH, WEB_FETCH_DESCRIPTION, WEB_SEARCH,
    WEB_SEARCH_DESCRIPTION,
};
pub use web_search::{
    parse_brave_results, parse_ddg_lite_html, parse_tavily_results, web_search, web_search_live,
    SearchHttpCall, HOST_DELEGATED_ERROR,
};
pub use write_lease::{
    acquire_write_lease, lock_path_for_db, LeaseError, StoreAlreadyOwnedError, WriteLease,
};
