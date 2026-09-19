//! `run_js` / `wait_js` registration and worker env (Python `ptc_js.py`).

use std::collections::HashMap;
use std::path::Path;

use serde_json::{json, Value};

use crate::run_code::{child_environ, env_truthy, refuse_nested, MAX_SOURCE_BYTES};
use crate::types::ToolResult;
use crate::web::tool_descriptor;

pub const RUN_JS: &str = "run_js";
pub const WAIT_JS: &str = "wait_js";
pub const PTC_JS_ENV: &str = "STEERABLE_PTC_JS";
pub const PTC_NODE_ENV: &str = "STEERABLE_PTC_NODE";
pub const DEFAULT_YIELD_MS: u64 = 10_000;
pub const DEFAULT_MAX_OUTPUT_CHARS: usize = 40_000;

pub const RUN_JS_DESCRIPTION: &str = "Run JavaScript code to orchestrate/compose tool calls in a persistent session.\n- The code runs as the body of an async function: top-level `await` works and `return <value>` is the cell's result.\n- Call other tools on the global `tools` object: `const r = await tools.bash({command: \"ls\"})` or `await tools.call(\"bash\", {command: \"ls\"})`. A successful call resolves with the tool's data payload; a failed one rejects the promise. Nested run_js/wait_js are refused.\n- Raw JavaScript only — no Node.js: no require/import, no process, no fs, no network, no Buffer.\n- `store(key, value)` / `load(key)`: a JSON-serializable KV shared by every run_js cell of this chat; values persist across cells and turns.\n- `text(value)` appends output returned with the result; `console.*` goes to the result's logs. `setTimeout`/`clearTimeout` exist; pending timers do not keep a cell alive. `exit()` ends the cell successfully and early.\n- `yield_control()` returns the accumulated output immediately while the cell keeps running. If the cell is still running after yieldTimeMs (default 10000), run_js returns early the same way: status \"running\" plus a cellId. Call wait_js with that cellId to get more output or the final result.";

pub const WAIT_JS_DESCRIPTION: &str = "Wait on a running run_js cell.\n- `cellId` identifies the running cell (from a run_js result with status \"running\").\n- Returns the output accumulated since the last response, or the cell's final result once it finishes; a finished cell is closed by reading its result.\n- `yieldTimeMs` (default 10000) bounds the wait before answering with the output so far.\n- `terminate: true` stops the cell and returns its final state.";

const WORKER_PASSTHROUGH: &[&str] = &[
    "ELECTRON_RUN_AS_NODE",
    "STEERABLE_PTC_JS_SESSION_TTL_MS",
    "STEERABLE_PTC_JS_MAX_SESSIONS",
];

pub fn ptc_js_enabled(environ: &HashMap<String, String>) -> bool {
    environ
        .get(PTC_JS_ENV)
        .map(|value| env_truthy(value))
        .unwrap_or(false)
}

pub fn worker_environ(environ: &HashMap<String, String>) -> HashMap<String, String> {
    let mut child = child_environ(environ);
    for key in WORKER_PASSTHROUGH {
        if let Some(value) = environ.get(*key) {
            if !value.is_empty() {
                child.insert((*key).to_string(), value.clone());
            }
        }
    }
    child
}

pub fn node_executable(environ: &HashMap<String, String>) -> Option<String> {
    let explicit = environ
        .get(PTC_NODE_ENV)
        .map(|value| value.trim())
        .filter(|value| !value.is_empty());
    if let Some(path) = explicit {
        return Some(path.to_string());
    }
    which_bin("node", environ.get("PATH").map(String::as_str))
}

fn which_bin(name: &str, path_override: Option<&str>) -> Option<String> {
    let path = match path_override {
        Some(value) => std::ffi::OsString::from(value),
        None => std::env::var_os("PATH")?,
    };
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(name);
        if candidate.is_file() {
            return Some(candidate.to_string_lossy().into_owned());
        }
        #[cfg(windows)]
        {
            let exe = dir.join(format!("{name}.exe"));
            if exe.is_file() {
                return Some(exe.to_string_lossy().into_owned());
            }
        }
    }
    None
}

pub fn node_unavailable(explicit: Option<&str>) -> ToolResult {
    let message = match explicit {
        Some(path) => format!("STEERABLE_PTC_NODE points at '{path}', which does not exist."),
        None => "run_js needs a Node.js runtime: no STEERABLE_PTC_NODE set and no `node` on PATH."
            .to_string(),
    };
    ToolResult::fail_closed_data("node_unavailable", json!({ "message": message }))
}

pub fn ptc_sandbox_unavailable() -> ToolResult {
    ToolResult::fail_closed_data(
        "sandbox_unavailable",
        json!({
            "_sandbox": {"backend": "none", "enforcement": "none"},
            "message": "Refused to start the run_js worker: no OS sandbox backend to confine it."
        }),
    )
}

pub fn run_js_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "JavaScript source, evaluated as the body of an async function. `return` is the cell result. Call tools with `await tools.<name>({...})`."
            },
            "description": {
                "type": "string",
                "description": "Short summary of what the cell does."
            },
            "yieldTimeMs": {
                "type": "integer",
                "description": "Return early with a cellId if the cell is still running after this many ms. Defaults to 10000."
            },
            "maxOutputChars": {
                "type": "integer",
                "description": "Output budget for this call's result, in characters. Defaults to 40000."
            }
        },
        "required": ["code"],
        "additionalProperties": false
    })
}

pub fn wait_js_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "cellId": {
                "type": "string",
                "description": "Identifier of the running run_js cell."
            },
            "yieldTimeMs": {
                "type": "integer",
                "description": "Wait at most this many ms for more output before answering. Defaults to 10000."
            },
            "maxOutputChars": {
                "type": "integer",
                "description": "Output budget for this call's result, in characters. Defaults to 40000."
            },
            "terminate": {
                "type": "boolean",
                "description": "True stops the running cell; false or omitted waits for output."
            }
        },
        "required": ["cellId"],
        "additionalProperties": false
    })
}

pub fn run_js_tool_descriptor() -> Value {
    tool_descriptor(RUN_JS, RUN_JS_DESCRIPTION, run_js_schema())
}

pub fn wait_js_tool_descriptor() -> Value {
    tool_descriptor(WAIT_JS, WAIT_JS_DESCRIPTION, wait_js_schema())
}

pub fn nested_ptc_refused(name: &str) -> Option<ToolResult> {
    refuse_nested(name, &[RUN_JS, WAIT_JS])
}

pub fn invoke_run_js(
    args: &Value,
    environ: &HashMap<String, String>,
    has_sandbox: bool,
) -> ToolResult {
    let code = args
        .get("code")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if code.is_empty() {
        return ToolResult::fail("code is empty");
    }
    if code.len() > MAX_SOURCE_BYTES {
        return ToolResult::fail("run_js source exceeds the size cap");
    }
    let _yield_ms = args
        .get("yieldTimeMs")
        .and_then(Value::as_u64)
        .filter(|ms| *ms > 0)
        .unwrap_or(DEFAULT_YIELD_MS);
    let _max_output = args
        .get("maxOutputChars")
        .and_then(Value::as_u64)
        .filter(|n| *n > 0)
        .map(|n| n as usize)
        .unwrap_or(DEFAULT_MAX_OUTPUT_CHARS);
    let _ = (_yield_ms, _max_output);
    match node_executable(environ) {
        Some(path) if Path::new(&path).exists() => {}
        Some(path) => return node_unavailable(Some(&path)),
        None => return node_unavailable(None),
    }
    if !has_sandbox {
        return ptc_sandbox_unavailable();
    }
    ToolResult::fail("run_js worker is not bundled in the rust sidecar yet")
}

pub fn invoke_wait_js(args: &Value) -> ToolResult {
    let cell_id = args
        .get("cellId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if cell_id.is_empty() {
        return ToolResult::fail("cellId is empty");
    }
    ToolResult::fail("run_js worker is not bundled in the rust sidecar yet")
}
