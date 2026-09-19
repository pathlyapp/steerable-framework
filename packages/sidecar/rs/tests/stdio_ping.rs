use std::io::Write;
use std::process::{Command, Stdio};
use std::time::Duration;

use serde_json::{json, Value};

fn spawn_sidecar() -> std::process::Child {
    Command::new(env!("CARGO_BIN_EXE_steerable-sidecar"))
        .env("STEERABLE_SIDECAR_FAKE_LLM", "1")
        .env("STEERABLE_SIDECAR_FAKE_REPLY", "pong from rust")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn rust sidecar")
}

fn read_ready(child: &mut std::process::Child) -> Value {
    let mut stderr = child.stderr.take().expect("stderr");
    let mut buf = String::new();
    let start = std::time::Instant::now();
    use std::io::Read;
    while start.elapsed() < Duration::from_secs(5) {
        let mut chunk = [0u8; 1024];
        let n = stderr.read(&mut chunk).unwrap_or(0);
        if n > 0 {
            buf.push_str(&String::from_utf8_lossy(&chunk[..n]));
            if let Some(line) = buf
                .lines()
                .find(|line| line.starts_with("__SIDECAR_READY__:"))
            {
                let json = line.trim().trim_start_matches("__SIDECAR_READY__:");
                child.stderr = Some(stderr);
                return serde_json::from_str(json).unwrap();
            }
        }
        if n == 0 {
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    panic!("ready marker not seen: {buf}");
}

fn write_frame(child: &mut std::process::Child, payload: &Value) {
    let stdin = child.stdin.as_mut().unwrap();
    writeln!(stdin, "{}", payload).unwrap();
    stdin.flush().unwrap();
}

fn read_json_line(stdout: &mut impl std::io::BufRead) -> Value {
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    serde_json::from_str(line.trim()).unwrap()
}

#[test]
fn rust_sidecar_ready_ping_shutdown_and_fake_chat() {
    let mut child = spawn_sidecar();
    let ready = read_ready(&mut child);
    assert_eq!(ready["status"], json!("ok"));
    assert_eq!(ready["engine"], json!("rust"));

    let stdout = child.stdout.take().expect("stdout");
    let mut stdout = std::io::BufReader::new(stdout);
    let lifecycle = read_json_line(&mut stdout);
    assert_eq!(lifecycle["method"], json!("lifecycle.ready"));

    write_frame(
        &mut child,
        &json!({"jsonrpc":"2.0","id":1,"method":"system.ping"}),
    );
    let ping = read_json_line(&mut stdout);
    assert_eq!(ping["id"], json!(1));
    assert_eq!(ping["result"]["status"], json!("ok"));
    assert_eq!(ping["result"]["checks"]["engine"]["message"], json!("rust"));

    write_frame(
        &mut child,
        &json!({"jsonrpc":"2.0","id":10,"method":"tool.list"}),
    );
    let listed = read_json_line(&mut stdout);
    let names: Vec<&str> = listed["result"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|entry| entry["function"]["name"].as_str())
        .collect();
    assert!(names.contains(&"web_fetch"), "{listed}");

    write_frame(
        &mut child,
        &json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "agent.chat.stream",
            "params": {"messages": [{"role": "user", "content": "hi"}]}
        }),
    );
    let started = read_json_line(&mut stdout);
    assert!(started["result"]["streamId"]
        .as_str()
        .unwrap()
        .starts_with("str_"));
    let mut saw_delta = false;
    let mut saw_done = false;
    for _ in 0..20 {
        let frame = read_json_line(&mut stdout);
        if frame["method"] == json!("stream.chunk")
            && frame["params"]["delta"] == json!("pong from rust")
        {
            saw_delta = true;
        }
        if frame["method"] == json!("stream.done") {
            assert_eq!(frame["params"]["ok"], json!(true));
            assert_eq!(frame["params"]["engine"], json!("rust"));
            saw_done = true;
            break;
        }
    }
    assert!(saw_delta && saw_done);

    write_frame(
        &mut child,
        &json!({"jsonrpc":"2.0","id":3,"method":"system.shutdown"}),
    );
    let shutdown = read_json_line(&mut stdout);
    assert_eq!(shutdown["result"], json!(null));
    let status = child.wait().unwrap();
    assert!(status.success());
}
