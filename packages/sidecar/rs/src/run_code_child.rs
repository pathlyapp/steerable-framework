//! Spawn the confined `run_code` Python driver and pump its JSON-IPC.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use steerable_agent_runtime::{
    child_environ, refuse_nested, sidecar_confined, timeout_ms, PumpAction, RunCodePump,
    ToolResult, RUN_CODE,
};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;

use crate::sandbox::{confine_exec, landlock_available, seatbelt_available, BWRAP_CANDIDATES};

const DRIVER_SOURCE: &str = include_str!("../../py/src/steerable_sidecar/run_code_driver.py");

type NestedFn = Box<
    dyn FnMut(
            String,
            Map<String, Value>,
        ) -> Pin<Box<dyn std::future::Future<Output = ToolResult> + Send>>
        + Send,
>;

pub fn python_executable(environ: &HashMap<String, String>) -> Option<PathBuf> {
    if let Some(path) = environ.get("STEERABLE_PYTHON") {
        let candidate = PathBuf::from(path);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    let path = environ.get("PATH").cloned().unwrap_or_default();
    let dirs: Vec<String> = if path.is_empty() {
        vec![
            "/usr/bin".into(),
            "/opt/homebrew/bin".into(),
            "/usr/local/bin".into(),
        ]
    } else {
        path.split(':').map(str::to_string).collect()
    };
    for dir in dirs {
        let candidate = Path::new(&dir).join("python3");
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

pub fn has_layer2_sandbox() -> bool {
    seatbelt_available()
        || BWRAP_CANDIDATES
            .iter()
            .any(|path| Path::new(path).is_file())
        || landlock_available()
}

pub async fn invoke_run_code_child(
    source: &str,
    description: &str,
    environ: &HashMap<String, String>,
    nested: NestedFn,
) -> ToolResult {
    let inherited = sidecar_confined(environ);
    if !inherited && !has_layer2_sandbox() {
        return steerable_agent_runtime::sandbox_unavailable();
    }
    let Some(python) = python_executable(environ) else {
        return ToolResult::fail("python interpreter not found for run_code child");
    };
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let tmp =
        std::env::temp_dir().join(format!("steerable-run-code-{}-{nanos}", std::process::id()));
    if let Err(error) = std::fs::create_dir_all(&tmp) {
        return ToolResult::fail(format!("run_code temp dir: {error}"));
    }
    let program_path = tmp.join("program.py");
    let driver_path = tmp.join("run_code_driver.py");
    if let Err(error) = std::fs::write(&program_path, source)
        .and_then(|_| std::fs::write(&driver_path, DRIVER_SOURCE))
    {
        let _ = std::fs::remove_dir_all(&tmp);
        return ToolResult::fail(format!("run_code write: {error}"));
    }
    let driver_argv = vec![
        python.to_string_lossy().into_owned(),
        driver_path.to_string_lossy().into_owned(),
        "--program".into(),
        program_path.to_string_lossy().into_owned(),
    ];
    let (argv, sandbox_marker) = if inherited {
        (
            driver_argv,
            json!({"backend": "inherited", "enforcement": "partial", "via": "layer1"}),
        )
    } else {
        match confine_exec(&driver_argv, &[tmp.to_string_lossy().into_owned()], false) {
            Ok(value) => value,
            Err(error) => {
                let _ = std::fs::remove_dir_all(&tmp);
                return ToolResult::fail(error);
            }
        }
    };
    let result = pump_child(&argv, description, environ, sandbox_marker, nested).await;
    let _ = std::fs::remove_dir_all(&tmp);
    result
}

async fn pump_child(
    argv: &[String],
    description: &str,
    environ: &HashMap<String, String>,
    sandbox_marker: Value,
    mut nested: NestedFn,
) -> ToolResult {
    let Some((exe, args)) = argv.split_first() else {
        return ToolResult::fail("run_code argv is empty");
    };
    let mut command = Command::new(exe);
    command.args(args);
    command.stdin(std::process::Stdio::piped());
    command.stdout(std::process::Stdio::piped());
    command.stderr(std::process::Stdio::piped());
    command.env_clear();
    for (key, value) in child_environ(environ) {
        command.env(key, value);
    }
    command.kill_on_drop(true);
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => return ToolResult::fail(format!("run_code spawn: {error}")),
    };
    let mut stdin = child.stdin.take().expect("stdin");
    let mut stdout = BufReader::new(child.stdout.take().expect("stdout"));
    let mut stderr = child.stderr.take().expect("stderr");
    let mut pump = RunCodePump::new(description, sandbox_marker);
    let deadline = Instant::now() + Duration::from_millis(timeout_ms(environ));
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            let _ = child.kill().await;
            return ToolResult::fail("run_code timed out");
        }
        let mut line = String::new();
        match tokio::time::timeout(remaining, stdout.read_line(&mut line)).await {
            Err(_) => {
                let _ = child.kill().await;
                return ToolResult::fail("run_code timed out");
            }
            Ok(Err(error)) => return ToolResult::fail(format!("run_code stdout: {error}")),
            Ok(Ok(0)) => {
                let mut err = Vec::new();
                let _ = stderr.read_to_end(&mut err).await;
                let _ = child.wait().await;
                let err = String::from_utf8_lossy(&err);
                if !err.trim().is_empty() {
                    return ToolResult::fail_with_data(
                        err.trim(),
                        json!({
                            "description": description,
                            "calls": [],
                            "logs": [],
                        }),
                    );
                }
                return match pump.on_line(&[]) {
                    PumpAction::Finished(result) => result,
                    _ => ToolResult::fail("run_code child exited without a result"),
                };
            }
            Ok(Ok(_)) => match pump.on_line(line.trim_end().as_bytes()) {
                PumpAction::Continue => {}
                PumpAction::Finished(result) => {
                    let _ = child.kill().await;
                    return result;
                }
                PumpAction::Nested {
                    id,
                    tool,
                    arguments,
                } => {
                    let result = if let Some(denied) = refuse_nested(&tool, &[RUN_CODE]) {
                        denied
                    } else {
                        nested(tool.clone(), arguments.clone()).await
                    };
                    let reply = pump.reply_for(id, &result);
                    pump.record_nested(tool, arguments, &result);
                    let payload = format!("{reply}\n");
                    if stdin.write_all(payload.as_bytes()).await.is_err() {
                        return ToolResult::fail("run_code IPC closed");
                    }
                }
            },
        }
    }
}
