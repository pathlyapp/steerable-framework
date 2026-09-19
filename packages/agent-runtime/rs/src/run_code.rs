//! Parent-side `run_code` protocol (Python `run_code.py` pump + env scrub).

use std::collections::HashMap;

use serde_json::{json, Map, Value};

use crate::types::ToolResult;
use crate::web::tool_descriptor;

pub const RUN_CODE: &str = "run_code";
pub const RUN_CODE_ENV: &str = "STEERABLE_RUN_CODE";
pub const RUN_CODE_TIMEOUT_ENV: &str = "STEERABLE_RUN_CODE_TIMEOUT_MS";
pub const SIDECAR_CONFINED_ENV: &str = "STEERABLE_SIDECAR_CONFINED";
pub const MAX_NESTED_CALLS: usize = 32;
pub const MAX_SOURCE_BYTES: usize = 100_000;
pub const DEFAULT_TIMEOUT_MS: u64 = 60_000;

const CHILD_ENV_ALLOWLIST: &[&str] = &[
    "PATH",
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "PYTHONPATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
];

pub const RUN_CODE_DESCRIPTION: &str = "Run a short Python program that can call other tools in this turn (tools.call / tools.<name>). Use it to chain several tool calls without extra model rounds. Native tools remain available.";

pub fn env_truthy(raw: &str) -> bool {
    matches!(
        raw.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

pub fn run_code_enabled(environ: &HashMap<String, String>) -> bool {
    environ
        .get(RUN_CODE_ENV)
        .map(|value| env_truthy(value))
        .unwrap_or(false)
}

pub fn sidecar_confined(environ: &HashMap<String, String>) -> bool {
    environ
        .get(SIDECAR_CONFINED_ENV)
        .map(|value| env_truthy(value))
        .unwrap_or(false)
}

pub fn timeout_ms(environ: &HashMap<String, String>) -> u64 {
    environ
        .get(RUN_CODE_TIMEOUT_ENV)
        .and_then(|raw| raw.trim().parse::<u64>().ok())
        .map(|ms| ms.max(1_000))
        .unwrap_or(DEFAULT_TIMEOUT_MS)
}

pub fn child_environ(environ: &HashMap<String, String>) -> HashMap<String, String> {
    let mut child = HashMap::new();
    for (key, value) in environ {
        let allowed = if cfg!(windows) {
            let upper = key.to_ascii_uppercase();
            CHILD_ENV_ALLOWLIST
                .iter()
                .any(|name| name.eq_ignore_ascii_case(&upper))
                || upper.starts_with("LC_")
        } else {
            CHILD_ENV_ALLOWLIST.contains(&key.as_str()) || key.starts_with("LC_")
        };
        if allowed {
            child.insert(key.clone(), value.clone());
        }
    }
    child.insert("PYTHONDONTWRITEBYTECODE".into(), "1".into());
    child
}

pub fn run_code_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Body of a Python function (indentation as the body). `return` is the tool result. Call other tools with `tools.call(\"bash\", command=\"ls\")` or `tools.bash(command=\"ls\")`. Nested run_code is refused. `os` / `subprocess` / `socket` cannot be imported."
            },
            "description": {
                "type": "string",
                "description": "Short summary of what the program does."
            }
        },
        "required": ["code", "description"],
        "additionalProperties": false
    })
}

pub fn run_code_tool_descriptor() -> Value {
    tool_descriptor(RUN_CODE, RUN_CODE_DESCRIPTION, run_code_schema())
}

pub fn refuse_nested(name: &str, refused: &[&str]) -> Option<ToolResult> {
    if refused.contains(&name) {
        Some(ToolResult::fail_closed(format!(
            "nested {name} is not allowed"
        )))
    } else {
        None
    }
}

pub fn sandbox_unavailable() -> ToolResult {
    ToolResult::fail_closed_data(
        "sandbox_unavailable",
        json!({
            "_sandbox": {"backend": "none", "enforcement": "none"},
            "message": "Refused to run run_code: no OS sandbox backend to confine the child interpreter."
        }),
    )
}

pub fn source_cap_error(source: &str) -> Option<ToolResult> {
    if source.len() > MAX_SOURCE_BYTES {
        Some(ToolResult::fail("run_code source exceeds the size cap"))
    } else {
        None
    }
}

fn result_payload(result: &ToolResult) -> Value {
    result.to_rpc()
}

pub enum PumpAction {
    Continue,
    Nested {
        id: Value,
        tool: String,
        arguments: Map<String, Value>,
    },
    Finished(ToolResult),
}

pub struct RunCodePump {
    description: String,
    sandbox_marker: Value,
    calls: Vec<Value>,
    logs: Vec<String>,
}

impl RunCodePump {
    pub fn new(description: impl Into<String>, sandbox_marker: Value) -> Self {
        Self {
            description: description.into(),
            sandbox_marker,
            calls: Vec::new(),
            logs: Vec::new(),
        }
    }

    pub fn on_line(&mut self, line: &[u8]) -> PumpAction {
        if line.is_empty() {
            return PumpAction::Finished(ToolResult::fail_with_data(
                "run_code child exited without a result",
                self.partial_data(),
            ));
        }
        let text = String::from_utf8_lossy(line);
        let Ok(frame) = serde_json::from_str::<Value>(text.trim()) else {
            self.logs.push(text.trim_end().to_string());
            return PumpAction::Continue;
        };
        match frame.get("type").and_then(Value::as_str) {
            Some("log") => {
                if let Some(log) = frame.get("text").and_then(Value::as_str) {
                    if !log.is_empty() {
                        self.logs.push(log.to_string());
                    }
                }
                PumpAction::Continue
            }
            Some("done") => {
                if frame.get("ok").and_then(Value::as_bool) == Some(true) {
                    PumpAction::Finished(ToolResult::ok(json!({
                        "description": self.description,
                        "value": frame.get("value").cloned().unwrap_or(Value::Null),
                        "calls": self.calls,
                        "logs": self.logs,
                        "_sandbox": self.sandbox_marker,
                    })))
                } else {
                    PumpAction::Finished(ToolResult::fail_with_data(
                        frame
                            .get("error")
                            .and_then(Value::as_str)
                            .unwrap_or("run_code failed"),
                        self.partial_data(),
                    ))
                }
            }
            Some("call") => {
                if self.calls.len() >= MAX_NESTED_CALLS {
                    return PumpAction::Finished(ToolResult::fail_with_data(
                        format!("run_code exceeded {MAX_NESTED_CALLS} nested tool calls"),
                        self.partial_data(),
                    ));
                }
                let tool = frame
                    .get("tool")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                let arguments = frame
                    .get("arguments")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                PumpAction::Nested {
                    id: frame.get("id").cloned().unwrap_or(Value::Null),
                    tool,
                    arguments,
                }
            }
            _ => PumpAction::Continue,
        }
    }

    pub fn record_nested(
        &mut self,
        tool: String,
        arguments: Map<String, Value>,
        result: &ToolResult,
    ) {
        self.calls.push(json!({
            "tool": tool,
            "arguments": arguments,
            "result": result_payload(result),
        }));
    }

    pub fn reply_for(&self, id: Value, result: &ToolResult) -> Value {
        json!({
            "v": 1,
            "id": id,
            "ok": result.success,
            "result": if result.success { result_payload(result) } else { Value::Null },
            "error": if result.success {
                Value::Null
            } else {
                json!(result.error.clone().unwrap_or_else(|| "tool failed".into()))
            },
        })
    }

    fn partial_data(&self) -> Value {
        json!({
            "description": self.description,
            "calls": self.calls,
            "logs": self.logs,
        })
    }
}

pub fn drive_scripted(
    description: &str,
    sandbox_marker: Value,
    inbound: &[Value],
    mut nested: impl FnMut(&str, &Map<String, Value>) -> ToolResult,
) -> (ToolResult, Vec<Value>) {
    let mut pump = RunCodePump::new(description, sandbox_marker);
    let mut replies = Vec::new();
    for frame in inbound {
        let line = serde_json::to_vec(frame).unwrap();
        match pump.on_line(&line) {
            PumpAction::Continue => {}
            PumpAction::Finished(result) => return (result, replies),
            PumpAction::Nested {
                id,
                tool,
                arguments,
            } => {
                let result =
                    refuse_nested(&tool, &[RUN_CODE]).unwrap_or_else(|| nested(&tool, &arguments));
                replies.push(pump.reply_for(id, &result));
                pump.record_nested(tool, arguments, &result);
            }
        }
    }
    (
        ToolResult::fail_with_data("run_code child exited without a result", json!({})),
        replies,
    )
}
