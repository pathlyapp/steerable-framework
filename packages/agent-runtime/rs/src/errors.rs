//! Provider error taxonomy — kinds RetryHooks and CompactionHooks route on.

const OVERFLOW_MARKERS: &[&str] = &[
    "context length",
    "context window",
    "context_length",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "reduce the length",
    "exceeds the context",
    "context size",
    "token limit",
];

const RETRY_AFTER_CAP_MS: u64 = 180_000;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LLMError {
    pub message: String,
    pub kind: LLMErrorKind,
    pub status_code: Option<u16>,
    pub provider: Option<String>,
    pub retry_after_ms: Option<u64>,
}

impl LLMError {
    pub fn new(message: impl Into<String>, kind: LLMErrorKind) -> Self {
        Self {
            message: message.into(),
            kind,
            status_code: None,
            provider: None,
            retry_after_ms: None,
        }
    }

    pub fn retryable(&self) -> bool {
        self.kind.retryable()
    }
}

impl std::fmt::Display for LLMError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for LLMError {}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LLMErrorKind {
    Transport,
    RateLimit,
    ContextOverflow,
    Auth,
    InvalidRequest,
    Server,
    Unknown,
}

impl LLMErrorKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Transport => "transport",
            Self::RateLimit => "rate_limit",
            Self::ContextOverflow => "context_overflow",
            Self::Auth => "auth",
            Self::InvalidRequest => "invalid_request",
            Self::Server => "server",
            Self::Unknown => "unknown",
        }
    }

    pub fn retryable(self) -> bool {
        matches!(
            self,
            Self::Transport | Self::RateLimit | Self::Server | Self::Unknown
        )
    }
}

pub fn parse_retry_after_ms(value: &str) -> Option<u64> {
    let raw = value.trim();
    if raw.is_empty() {
        return None;
    }
    let seconds: f64 = raw.parse().ok()?;
    if seconds <= 0.0 {
        return None;
    }
    Some(((seconds * 1000.0) as u64).min(RETRY_AFTER_CAP_MS))
}

pub fn classify_http_status(status_code: u16, body: &str) -> LLMErrorKind {
    let lowered = body.to_ascii_lowercase();
    let looks_overflow = OVERFLOW_MARKERS
        .iter()
        .any(|marker| lowered.contains(marker));
    if matches!(status_code, 400 | 413) && looks_overflow {
        return LLMErrorKind::ContextOverflow;
    }
    if matches!(status_code, 401 | 403) {
        return LLMErrorKind::Auth;
    }
    if status_code == 429 {
        return LLMErrorKind::RateLimit;
    }
    if status_code == 408 {
        return LLMErrorKind::Transport;
    }
    if (500..600).contains(&status_code) {
        return LLMErrorKind::Server;
    }
    if (400..500).contains(&status_code) {
        return LLMErrorKind::InvalidRequest;
    }
    LLMErrorKind::Unknown
}
