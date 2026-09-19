use std::io::{Read, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::Value;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_steerable-sidecar")
}

fn tmp_dir() -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let path = std::env::temp_dir().join(format!(
        "steerable-sidecar-lease-{}-{nanos}",
        std::process::id()
    ));
    std::fs::create_dir_all(&path).unwrap();
    path
}

fn spawn_with_storage(db: &std::path::Path) -> std::process::Child {
    Command::new(bin())
        .arg("--storage-path")
        .arg(db)
        .env("STEERABLE_SIDECAR_FAKE_LLM", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn rust sidecar")
}

fn read_ready(stderr: &mut impl Read, timeout: Duration) -> Result<Value, String> {
    let mut buf = String::new();
    let start = Instant::now();
    while start.elapsed() < timeout {
        let mut chunk = [0u8; 1024];
        let n = stderr.read(&mut chunk).unwrap_or(0);
        if n > 0 {
            buf.push_str(&String::from_utf8_lossy(&chunk[..n]));
            if let Some(line) = buf
                .lines()
                .find(|line| line.starts_with("__SIDECAR_READY__:"))
            {
                let json = line.trim().trim_start_matches("__SIDECAR_READY__:");
                return serde_json::from_str(json).map_err(|err| err.to_string());
            }
            if buf.contains("store already owned") {
                return Err(buf);
            }
        } else if buf.contains("store already owned") {
            return Err(buf);
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    Err(format!("ready marker not seen: {buf}"))
}

#[test]
fn second_sidecar_fails_loud_then_successor_starts() {
    let dir = tmp_dir();
    let db = dir.join("sessions.db");
    let mut first = spawn_with_storage(&db);
    let mut first_err = first.stderr.take().expect("stderr");
    let ready = read_ready(&mut first_err, Duration::from_secs(5)).expect("first ready");
    assert_eq!(ready["engine"], serde_json::json!("rust"));
    first.stderr = Some(first_err);

    let mut second = spawn_with_storage(&db);
    let mut second_err = second.stderr.take().expect("second stderr");
    let denied = read_ready(&mut second_err, Duration::from_secs(5));
    assert!(
        denied
            .as_ref()
            .err()
            .map(|msg| msg.contains("already owned"))
            .unwrap_or(false),
        "second sidecar must fail the write lease, got {denied:?}"
    );
    let status = second.wait().expect("second wait");
    assert!(!status.success(), "second sidecar exit {status}");

    first.kill().ok();
    let _ = first.wait();

    let mut third = spawn_with_storage(&db);
    let mut third_err = third.stderr.take().expect("third stderr");
    let ready_again = read_ready(&mut third_err, Duration::from_secs(5)).expect("successor ready");
    assert_eq!(ready_again["engine"], serde_json::json!("rust"));
    write!(
        third.stdin.as_mut().unwrap(),
        "{}\n",
        r#"{"jsonrpc":"2.0","id":1,"method":"system.shutdown"}"#
    )
    .unwrap();
    let _ = third.wait();
    let _ = std::fs::remove_dir_all(&dir);
}
