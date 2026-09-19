//! Persistent Node worker for `run_js` / `wait_js` (Python `ptc_js.py`).

use std::collections::HashMap;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use steerable_agent_runtime::{
    nested_ptc_refused, node_executable, node_unavailable, ptc_sandbox_unavailable,
    sidecar_confined, worker_environ, ToolResult, DEFAULT_MAX_OUTPUT_CHARS, DEFAULT_YIELD_MS,
    MAX_SOURCE_BYTES,
};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::mpsc;

use crate::run_code_child::has_layer2_sandbox;
use crate::sandbox::confine_exec;

const WORKER_SOURCE: &str = include_str!("../../py/src/steerable_sidecar/ptc_js_worker.cjs");
const READY_TIMEOUT: Duration = Duration::from_secs(15);
const DEFAULT_CELL_TIMEOUT_MS: u64 = 600_000;
const DEFAULT_MARGIN_MS: u64 = 60_000;
const MAX_TOOL_CALLS: u64 = 32;

type NestedFn = Arc<
    dyn Fn(
            String,
            Map<String, Value>,
        ) -> Pin<Box<dyn std::future::Future<Output = ToolResult> + Send>>
        + Send
        + Sync,
>;

struct CellState {
    description: String,
    tx: mpsc::UnboundedSender<Value>,
    calls: Vec<Value>,
}

struct Worker {
    stdin: Arc<tokio::sync::Mutex<ChildStdin>>,
    child: Child,
    dead: Arc<AtomicBool>,
}

struct PtcRuntime {
    worker: Option<Worker>,
    cells: HashMap<String, CellState>,
    sandbox_marker: Value,
    nested: Option<NestedFn>,
    worker_path: Option<PathBuf>,
}

static CELL_SEQ: AtomicU64 = AtomicU64::new(1);

fn runtime() -> &'static tokio::sync::Mutex<PtcRuntime> {
    static CELL: OnceLock<tokio::sync::Mutex<PtcRuntime>> = OnceLock::new();
    CELL.get_or_init(|| {
        tokio::sync::Mutex::new(PtcRuntime {
            worker: None,
            cells: HashMap::new(),
            sandbox_marker: Value::Null,
            nested: None,
            worker_path: None,
        })
    })
}

fn next_cell_id() -> String {
    format!("cell-{:016x}", CELL_SEQ.fetch_add(1, Ordering::Relaxed))
}

fn yield_ms(args: &Value) -> u64 {
    args.get("yieldTimeMs")
        .and_then(Value::as_u64)
        .filter(|ms| *ms > 0)
        .unwrap_or(DEFAULT_YIELD_MS)
}

fn max_output_chars(args: &Value) -> usize {
    args.get("maxOutputChars")
        .and_then(Value::as_u64)
        .filter(|n| *n > 0)
        .map(|n| n as usize)
        .unwrap_or(DEFAULT_MAX_OUTPUT_CHARS)
}

fn cell_timeout_ms(environ: &HashMap<String, String>) -> u64 {
    environ
        .get("STEERABLE_PTC_JS_CELL_TIMEOUT_MS")
        .and_then(|raw| raw.trim().parse().ok())
        .filter(|ms: &u64| *ms > 0)
        .unwrap_or(DEFAULT_CELL_TIMEOUT_MS)
}

fn session_id(args: &Value) -> String {
    args.get("chatId")
        .or_else(|| args.get("chat_id"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or("default")
        .to_string()
}

fn write_worker_script() -> Result<PathBuf, String> {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let dir = std::env::temp_dir().join(format!("steerable-ptc-js-{}-{nanos}", std::process::id()));
    std::fs::create_dir_all(&dir).map_err(|err| err.to_string())?;
    let path = dir.join("ptc_js_worker.cjs");
    std::fs::write(&path, WORKER_SOURCE).map_err(|err| err.to_string())?;
    Ok(path)
}

pub async fn invoke_run_js_live(
    args: &Value,
    environ: &HashMap<String, String>,
    nested: NestedFn,
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
    let description = args
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("run_js")
        .to_string();
    let yield_time = yield_ms(args);
    let max_output = max_output_chars(args);
    let cell_id = next_cell_id();
    let (tx, rx) = mpsc::unbounded_channel();
    let stdin = {
        let mut guard = runtime().lock().await;
        guard.nested = Some(nested);
        if let Err(result) = guard.ensure_worker(environ).await {
            return result;
        }
        let stdin = guard.worker.as_ref().expect("worker").stdin.clone();
        guard.cells.insert(
            cell_id.clone(),
            CellState {
                description: description.clone(),
                tx,
                calls: Vec::new(),
            },
        );
        stdin
    };
    let frame = json!({
        "v": 1,
        "type": "exec",
        "cellId": cell_id,
        "sessionId": session_id(args),
        "code": code,
        "yieldTimeMs": yield_time,
        "maxOutputChars": max_output,
        "maxCellTimeMs": cell_timeout_ms(environ),
        "maxToolCalls": MAX_TOOL_CALLS,
        "tools": [
            {"name": "todo_write", "description": ""},
            {"name": "web_fetch", "description": ""}
        ],
    });
    if let Err(result) = write_frame(&stdin, &frame).await {
        let mut guard = runtime().lock().await;
        guard.cells.remove(&cell_id);
        return result;
    }
    await_cell(&cell_id, rx, yield_time, description).await
}

pub async fn invoke_wait_js_live(args: &Value, environ: &HashMap<String, String>) -> ToolResult {
    let cell_id = args
        .get("cellId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if cell_id.is_empty() {
        return ToolResult::fail("cellId is empty");
    }
    let yield_time = yield_ms(args);
    let max_output = max_output_chars(args);
    let terminate = args
        .get("terminate")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let stdin = {
        let mut guard = runtime().lock().await;
        if !guard.cells.contains_key(&cell_id) {
            return ToolResult::fail(format!(
                "unknown cell: {cell_id} (already finished, terminated, or lost with a restarted worker)"
            ));
        }
        if let Err(result) = guard.ensure_worker(environ).await {
            return result;
        }
        let stdin = guard.worker.as_ref().expect("worker").stdin.clone();
        let description = guard
            .cells
            .get(&cell_id)
            .map(|cell| cell.description.clone())
            .unwrap_or_default();
        let (tx, rx) = mpsc::unbounded_channel();
        if let Some(cell) = guard.cells.get_mut(&cell_id) {
            cell.tx = tx;
        }
        (stdin, description, rx)
    };
    let frame = json!({
        "v": 1,
        "type": "wait",
        "cellId": cell_id,
        "yieldTimeMs": yield_time,
        "maxOutputChars": max_output,
        "terminate": if terminate { Value::Bool(true) } else { Value::Null },
    });
    if let Err(result) = write_frame(&stdin.0, &frame).await {
        return result;
    }
    await_cell(&cell_id, stdin.2, yield_time, stdin.1).await
}

impl PtcRuntime {
    async fn ensure_worker(&mut self, environ: &HashMap<String, String>) -> Result<(), ToolResult> {
        if self
            .worker
            .as_ref()
            .is_some_and(|worker| !worker.dead.load(Ordering::SeqCst))
        {
            return Ok(());
        }
        self.worker = None;
        self.spawn_worker(environ).await
    }

    async fn spawn_worker(&mut self, environ: &HashMap<String, String>) -> Result<(), ToolResult> {
        let inherited = sidecar_confined(environ);
        if !inherited && !has_layer2_sandbox() {
            return Err(ptc_sandbox_unavailable());
        }
        let Some(node) = node_executable(environ) else {
            return Err(node_unavailable(None));
        };
        if !PathBuf::from(&node).is_file() {
            return Err(node_unavailable(Some(&node)));
        }
        if self.worker_path.is_none() {
            match write_worker_script() {
                Ok(path) => self.worker_path = Some(path),
                Err(error) => {
                    return Err(ToolResult::fail_closed_data(
                        "node_unavailable",
                        json!({"message": format!("Failed to materialize the run_js worker: {error}")}),
                    ));
                }
            }
        }
        let worker_path = self.worker_path.as_ref().unwrap().clone();
        let driver_argv = vec![node.clone(), worker_path.to_string_lossy().into_owned()];
        let (argv, marker) = if inherited {
            (
                driver_argv,
                json!({"backend": "inherited", "enforcement": "partial", "via": "layer1"}),
            )
        } else {
            match confine_exec(&driver_argv, &[], false) {
                Ok(value) => value,
                Err(error) => return Err(ToolResult::fail(error)),
            }
        };
        let Some((exe, args)) = argv.split_first() else {
            return Err(ToolResult::fail("run_js argv is empty"));
        };
        let mut command = Command::new(exe);
        command.args(args);
        command.stdin(std::process::Stdio::piped());
        command.stdout(std::process::Stdio::piped());
        command.stderr(std::process::Stdio::piped());
        command.env_clear();
        for (key, value) in worker_environ(environ) {
            command.env(key, value);
        }
        command.kill_on_drop(true);
        let mut child = command.spawn().map_err(|error| {
            ToolResult::fail_closed_data(
                "node_unavailable",
                json!({"message": format!("Failed to spawn the run_js worker ({node:?}): {error}")}),
            )
        })?;
        let stdin = child.stdin.take().expect("stdin");
        let stdout = child.stdout.take().expect("stdout");
        let dead = Arc::new(AtomicBool::new(false));
        let stdin = Arc::new(tokio::sync::Mutex::new(stdin));
        let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(read_loop(stdout, stdin.clone(), dead.clone(), ready_tx));
        match tokio::time::timeout(READY_TIMEOUT, ready_rx).await {
            Ok(Ok(true)) => {}
            Ok(Ok(false)) | Err(_) | Ok(Err(_)) => {
                let _ = child.kill().await;
                dead.store(true, Ordering::SeqCst);
                return Err(ToolResult::fail_closed_data(
                    "node_unavailable",
                    json!({
                        "message": format!(
                            "The run_js worker did not report ready within {}s. Check STEERABLE_PTC_NODE ({node:?}).",
                            READY_TIMEOUT.as_secs()
                        )
                    }),
                ));
            }
        }
        self.sandbox_marker = marker;
        self.worker = Some(Worker { stdin, child, dead });
        Ok(())
    }
}

async fn write_frame(
    stdin: &Arc<tokio::sync::Mutex<ChildStdin>>,
    frame: &Value,
) -> Result<(), ToolResult> {
    let payload = format!("{frame}\n");
    let mut stdin = stdin.lock().await;
    stdin
        .write_all(payload.as_bytes())
        .await
        .map_err(|_| ToolResult::fail("run_js worker pipe is closed"))
}

async fn read_loop(
    stdout: tokio::process::ChildStdout,
    stdin: Arc<tokio::sync::Mutex<ChildStdin>>,
    dead: Arc<AtomicBool>,
    ready_tx: tokio::sync::oneshot::Sender<bool>,
) {
    let mut lines = BufReader::new(stdout).lines();
    let mut ready = Some(ready_tx);
    while let Ok(Some(line)) = lines.next_line().await {
        let Ok(frame) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        match frame.get("type").and_then(Value::as_str) {
            Some("ready") => {
                if let Some(tx) = ready.take() {
                    let _ = tx.send(true);
                }
            }
            Some("tool_call") => {
                let call_id = frame
                    .get("callId")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                let cell_id = frame
                    .get("cellId")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
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
                let stdin = stdin.clone();
                tokio::spawn(async move {
                    bridge_tool(cell_id, call_id, tool, arguments, stdin).await;
                });
            }
            Some("cell_yield" | "cell_done") => {
                if let Some(cell_id) = frame.get("cellId").and_then(Value::as_str) {
                    let guard = runtime().lock().await;
                    if let Some(cell) = guard.cells.get(cell_id) {
                        let _ = cell.tx.send(frame);
                    }
                }
            }
            _ => {}
        }
    }
    dead.store(true, Ordering::SeqCst);
    if let Some(tx) = ready.take() {
        let _ = tx.send(false);
    }
    let guard = runtime().lock().await;
    for cell in guard.cells.values() {
        let _ = cell.tx.send(json!({"type": "worker_dead"}));
    }
}

async fn bridge_tool(
    cell_id: String,
    call_id: String,
    tool: String,
    arguments: Map<String, Value>,
    stdin: Arc<tokio::sync::Mutex<ChildStdin>>,
) {
    let result = if let Some(denied) = nested_ptc_refused(&tool) {
        denied
    } else {
        let nested = {
            let guard = runtime().lock().await;
            guard.nested.clone()
        };
        match nested {
            Some(nested) => nested(tool.clone(), arguments.clone()).await,
            None => ToolResult::fail("no tool executor bound for nested calls"),
        }
    };
    {
        let mut guard = runtime().lock().await;
        if let Some(cell) = guard.cells.get_mut(&cell_id) {
            cell.calls.push(json!({
                "tool": tool,
                "arguments": arguments,
                "result": result.to_rpc(),
            }));
        }
    }
    let reply = if result.success {
        json!({
            "v": 1,
            "type": "tool_result",
            "callId": call_id,
            "ok": true,
            "value": result.data.clone().unwrap_or(Value::Null).to_string(),
        })
    } else {
        json!({
            "v": 1,
            "type": "tool_result",
            "callId": call_id,
            "ok": false,
            "error": result.error.clone().unwrap_or_else(|| "tool failed".into()),
        })
    };
    let payload = format!("{reply}\n");
    let mut stdin = stdin.lock().await;
    let _ = stdin.write_all(payload.as_bytes()).await;
}

async fn await_cell(
    cell_id: &str,
    mut rx: mpsc::UnboundedReceiver<Value>,
    yield_time: u64,
    description: String,
) -> ToolResult {
    let timeout = Duration::from_millis(yield_time.max(1) + DEFAULT_MARGIN_MS);
    match tokio::time::timeout(timeout, rx.recv()).await {
        Ok(Some(frame)) => render_cell(cell_id, description, frame).await,
        Ok(None) => fail_dead(cell_id, description).await,
        Err(_) => {
            let mut guard = runtime().lock().await;
            if let Some(worker) = guard.worker.as_mut() {
                worker.dead.store(true, Ordering::SeqCst);
                let _ = worker.child.kill().await;
            }
            guard.worker = None;
            let calls = guard
                .cells
                .remove(cell_id)
                .map(|cell| json!(cell.calls))
                .unwrap_or(json!([]));
            ToolResult::fail_with_data(
                "run_js worker stopped responding (likely an uninterruptible synchronous loop in the cell); the worker was killed and all its cells were terminated",
                json!({"cellId": cell_id, "calls": calls}),
            )
        }
    }
}

async fn fail_dead(cell_id: &str, description: String) -> ToolResult {
    let mut guard = runtime().lock().await;
    let calls = guard
        .cells
        .remove(cell_id)
        .map(|cell| json!(cell.calls))
        .unwrap_or(json!([]));
    let _ = description;
    ToolResult::fail_with_data(
        "run_js worker died; the cell was lost with it",
        json!({"cellId": cell_id, "calls": calls}),
    )
}

async fn render_cell(cell_id: &str, description: String, frame: Value) -> ToolResult {
    let mut guard = runtime().lock().await;
    let marker = guard.sandbox_marker.clone();
    let calls = guard
        .cells
        .get(cell_id)
        .map(|cell| json!(cell.calls.clone()))
        .unwrap_or(json!([]));
    let mut base = json!({
        "cellId": cell_id,
        "description": description,
        "output": frame.get("output").cloned().unwrap_or(json!([])),
        "logs": frame.get("logs").cloned().unwrap_or(json!([])),
        "calls": calls,
        "_sandbox": marker,
    });
    if frame.get("truncated").and_then(Value::as_bool) == Some(true) {
        base["truncated"] = json!(true);
    }
    match frame.get("type").and_then(Value::as_str) {
        Some("worker_dead") => {
            guard.cells.remove(cell_id);
            ToolResult::fail_with_data(
                "run_js worker died; the cell was lost with it",
                json!({"cellId": cell_id, "calls": base["calls"]}),
            )
        }
        Some("cell_yield") => ToolResult::ok(json!({
            "cellId": cell_id,
            "description": description,
            "output": base["output"],
            "logs": base["logs"],
            "calls": base["calls"],
            "_sandbox": marker,
            "status": "running",
            "message": format!("Script running with cell ID {cell_id}. Call wait_js with this cellId to get more output or the final result."),
        })),
        _ => {
            guard.cells.remove(cell_id);
            if frame.get("ok").and_then(Value::as_bool) == Some(true) {
                let value = match frame.get("value") {
                    Some(Value::String(raw)) => serde_json::from_str(raw).unwrap_or(json!(raw)),
                    Some(other) => other.clone(),
                    None => Value::Null,
                };
                base["status"] = json!("completed");
                base["value"] = value;
                ToolResult::ok(base)
            } else {
                base["status"] = json!(if frame.get("terminated").and_then(Value::as_bool)
                    == Some(true)
                {
                    "terminated"
                } else {
                    "failed"
                });
                ToolResult::fail_with_data(
                    frame
                        .get("error")
                        .and_then(Value::as_str)
                        .unwrap_or("run_js cell failed"),
                    base,
                )
            }
        }
    }
}
