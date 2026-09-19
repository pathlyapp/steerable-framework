//! OpenAI-compatible vendor flag matrix (Python `llm.compat`).

use serde_json::{json, Value};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OpenAICompatFlags {
    pub supports_usage_in_streaming: bool,
    pub max_tokens_field: &'static str,
    pub supports_reasoning_effort: bool,
    pub supports_temperature: bool,
    pub supports_forced_tool_choice: bool,
    pub reasoning_delta_fields: &'static [&'static str],
    pub reasoning_echo_field: &'static str,
    pub echo_empty_reasoning_for_tool_calls: bool,
    pub cached_tokens_fields: &'static [&'static str],
}

impl Default for OpenAICompatFlags {
    fn default() -> Self {
        Self {
            supports_usage_in_streaming: true,
            max_tokens_field: "max_tokens",
            supports_reasoning_effort: true,
            supports_temperature: true,
            supports_forced_tool_choice: true,
            reasoning_delta_fields: &["reasoning_content", "reasoning"],
            reasoning_echo_field: "reasoning",
            echo_empty_reasoning_for_tool_calls: false,
            cached_tokens_fields: &[
                "prompt_tokens_details.cached_tokens",
                "prompt_cache_hit_tokens",
            ],
        }
    }
}

const DEEPSEEK: OpenAICompatFlags = OpenAICompatFlags {
    supports_usage_in_streaming: true,
    max_tokens_field: "max_tokens",
    supports_reasoning_effort: true,
    supports_temperature: true,
    supports_forced_tool_choice: false,
    reasoning_delta_fields: &["reasoning_content", "reasoning"],
    reasoning_echo_field: "reasoning_content",
    echo_empty_reasoning_for_tool_calls: true,
    cached_tokens_fields: &[
        "prompt_cache_hit_tokens",
        "prompt_tokens_details.cached_tokens",
    ],
};

const MOONSHOT: OpenAICompatFlags = OpenAICompatFlags {
    supports_usage_in_streaming: true,
    max_tokens_field: "max_tokens",
    supports_reasoning_effort: false,
    supports_temperature: false,
    supports_forced_tool_choice: true,
    reasoning_delta_fields: &["reasoning_content", "reasoning"],
    reasoning_echo_field: "reasoning",
    echo_empty_reasoning_for_tool_calls: false,
    cached_tokens_fields: &[
        "prompt_tokens_details.cached_tokens",
        "prompt_cache_hit_tokens",
    ],
};

const OPENROUTER: OpenAICompatFlags = OpenAICompatFlags {
    supports_usage_in_streaming: true,
    max_tokens_field: "max_tokens",
    supports_reasoning_effort: true,
    supports_temperature: true,
    supports_forced_tool_choice: true,
    reasoning_delta_fields: &["reasoning", "reasoning_content"],
    reasoning_echo_field: "reasoning",
    echo_empty_reasoning_for_tool_calls: false,
    cached_tokens_fields: &[
        "prompt_tokens_details.cached_tokens",
        "prompt_cache_hit_tokens",
    ],
};

const DASHSCOPE: OpenAICompatFlags = OpenAICompatFlags {
    supports_usage_in_streaming: true,
    max_tokens_field: "max_tokens",
    supports_reasoning_effort: true,
    supports_temperature: true,
    supports_forced_tool_choice: true,
    reasoning_delta_fields: &["reasoning_content", "reasoning"],
    reasoning_echo_field: "reasoning",
    echo_empty_reasoning_for_tool_calls: false,
    cached_tokens_fields: &[
        "prompt_tokens_details.cached_tokens",
        "prompt_cache_hit_tokens",
    ],
};

const PROVIDER_COMPAT_HOSTS: &[(&str, OpenAICompatFlags)] = &[
    ("api.deepseek.com", DEEPSEEK),
    ("api.moonshot.cn", MOONSHOT),
    ("api.moonshot.ai", MOONSHOT),
    ("openrouter.ai", OPENROUTER),
    ("dashscope.aliyuncs.com", DASHSCOPE),
    ("dashscope-intl.aliyuncs.com", DASHSCOPE),
];

pub fn compat_for_base_url(base_url: &str) -> Option<OpenAICompatFlags> {
    PROVIDER_COMPAT_HOSTS
        .iter()
        .find(|(host, _)| base_url.contains(host))
        .map(|(_, flags)| flags.clone())
}

pub fn describe_compat_flags() -> Vec<Value> {
    let defaults = OpenAICompatFlags::default();
    vec![
        json!({"key":"supportsUsageInStreaming","field":"supports_usage_in_streaming","kind":"bool","default":defaults.supports_usage_in_streaming,"description":"Send stream_options.include_usage; disable for vendors that reject it"}),
        json!({"key":"maxTokensField","field":"max_tokens_field","kind":"enum:max_tokens,max_completion_tokens","default":defaults.max_tokens_field,"description":"Request field that caps the response length"}),
        json!({"key":"supportsReasoningEffort","field":"supports_reasoning_effort","kind":"bool","default":defaults.supports_reasoning_effort,"description":"Send reasoning_effort when the env clamp yields a level"}),
        json!({"key":"supportsTemperature","field":"supports_temperature","kind":"bool","default":defaults.supports_temperature,"description":"Send temperature; disable for fixed-temperature reasoning models"}),
        json!({"key":"reasoningDeltaFields","field":"reasoning_delta_fields","kind":"string-list","default":defaults.reasoning_delta_fields,"description":"Delta keys read as reasoning text, in preference order"}),
        json!({"key":"reasoningEchoField","field":"reasoning_echo_field","kind":"string","default":defaults.reasoning_echo_field,"description":"Message key for echoing plaintext reasoning back to the vendor"}),
        json!({"key":"supportsForcedToolChoice","field":"supports_forced_tool_choice","kind":"bool","default":defaults.supports_forced_tool_choice,"description":"Accept tool_choice=required; disable to downgrade it to auto"}),
        json!({"key":"echoEmptyReasoningForToolCalls","field":"echo_empty_reasoning_for_tool_calls","kind":"bool","default":defaults.echo_empty_reasoning_for_tool_calls,"description":"Echo the reasoning field on tool-call turns that produced no reasoning"}),
        json!({"key":"cachedTokensFields","field":"cached_tokens_fields","kind":"string-list","default":defaults.cached_tokens_fields,"description":"Usage locations read for cached prompt tokens"}),
    ]
}
