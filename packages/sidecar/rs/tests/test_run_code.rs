use std::io::Write;
use std::process::{Command, Stdio};
use std::time::Duration;

use serde_json::{json, Value};

fn spawn_sidecar() -> std::process::Child {
    Command::new(env!("CARGO_BIN_EXE_steerable-sidecar"))
        .env("STEERABLE_RUN_CODE", "1")
        .env("STEERABLE_SIDECAR_FAKE_LLM", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn rust sidecar")
}

fn read_ready(child: &mut std::process::Child) {
    let mut stderr = child.stderr.take().expect("stderr");
    let mut buf = String::new();
    let start = std::time::Instant::now();
    use std::io::Read;
    while start.elapsed() < Duration::from_secs(5) {
        let mut chunk = [0u8; 1024];
        let n = stderr.read(&mut chunk).unwrap_or(0);
        if n > 0 {
            buf.push_str(&String::from_utf8_lossy(&chunk[..n]));
            if buf
                .lines()
                .any(|line| line.starts_with("__SIDECAR_READY__:"))
            {
                child.stderr = Some(stderr);
                return;
            }
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    panic!("ready marker not seen: {buf}");
}

fn write_frame(child: &mut std::process::Child, payload: &Value) {
    let stdin = child.stdin.as_mut().unwrap();
    writeln!(stdin, "{payload}").unwrap();
    stdin.flush().unwrap();
}

fn read_json_line(stdout: &mut impl std::io::BufRead) -> Value {
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    serde_json::from_str(line.trim()).unwrap()
}

#[test]
fn run_code_is_listed_when_enabled_and_returns_a_value() {
    let mut child = spawn_sidecar();
    read_ready(&mut child);
    let stdout = child.stdout.take().expect("stdout");
    let mut stdout = std::io::BufReader::new(stdout);
    let _lifecycle = read_json_line(&mut stdout);

    write_frame(
        &mut child,
        &json!({"jsonrpc":"2.0","id":1,"method":"tool.list"}),
    );
    let listed = read_json_line(&mut stdout);
    let names: Vec<&str> = listed["result"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|entry| entry["function"]["name"].as_str())
        .collect();
    assert!(names.contains(&"run_code"), "{listed}");

    write_frame(
        &mut child,
        &json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tool.invoke",
            "params": {
                "name": "run_code",
                "arguments": {
                    "code": "return {\"n\": 1 + 1}",
                    "description": "add"
                }
            }
        }),
    );
    let reply = read_json_line(&mut stdout);
    assert_eq!(reply["result"]["success"], json!(true), "{reply}");
    assert_eq!(reply["result"]["data"]["value"]["n"], json!(2), "{reply}");
    assert!(reply["result"]["data"]["_sandbox"]["backend"].is_string());

    write_frame(
        &mut child,
        &json!({"jsonrpc":"2.0","id":3,"method":"system.shutdown"}),
    );
    let _ = read_json_line(&mut stdout);
    let _ = child.wait();
}

#[test]
fn run_code_refuses_empty_source() {
    let mut child = spawn_sidecar();
    read_ready(&mut child);
    let stdout = child.stdout.take().expect("stdout");
    let mut stdout = std::io::BufReader::new(stdout);
    let _lifecycle = read_json_line(&mut stdout);
    write_frame(
        &mut child,
        &json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tool.invoke",
            "params": {
                "name": "run_code",
                "arguments": {"code": "   ", "description": "empty"}
            }
        }),
    );
    let reply = read_json_line(&mut stdout);
    assert_eq!(reply["result"]["success"], json!(false), "{reply}");
    assert!(
        reply["result"]["error"]
            .as_str()
            .unwrap_or("")
            .contains("empty"),
        "{reply}"
    );
    write_frame(
        &mut child,
        &json!({"jsonrpc":"2.0","id":3,"method":"system.shutdown"}),
    );
    let _ = child.wait();
}
