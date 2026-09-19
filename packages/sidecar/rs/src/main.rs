//! `steerable-sidecar` stdio JSON-RPC server.

use std::io::{self, Write};
use std::path::PathBuf;

use serde_json::json;
use steerable_agent_runtime::acquire_write_lease;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

use steerable_sidecar::methods::{
    dispatch, run_turn, Dispatch, SidecarState, PROTOCOL_VERSION, SIDECAR_VERSION,
};
use steerable_sidecar::rpc::{encode_frame, notification};
use steerable_sidecar::sandbox;

fn ready_marker() -> String {
    let payload = json!({
        "status": "ok",
        "version": SIDECAR_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "pid": std::process::id(),
        "engine": "rust",
    });
    format!("__SIDECAR_READY__:{payload}\n")
}

enum Mode {
    Serve {
        quiet: bool,
        storage_path: Option<PathBuf>,
    },
    Sandbox(Vec<String>),
}

fn parse_args() -> Mode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("sandbox") {
        return Mode::Sandbox(args);
    }
    let mut quiet = false;
    let mut storage_path = None;
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--quiet-ready" => quiet = true,
            "--storage-path" => {
                index += 1;
                storage_path = args.get(index).cloned().map(PathBuf::from);
            }
            flag if flag.starts_with("--storage-path=") => {
                storage_path = Some(PathBuf::from(flag.trim_start_matches("--storage-path=")));
            }
            _ => {}
        }
        index += 1;
    }
    Mode::Serve {
        quiet,
        storage_path,
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    match parse_args() {
        Mode::Sandbox(args) => std::process::exit(sandbox::sandbox_cli(&args)),
        Mode::Serve {
            quiet,
            storage_path,
        } => serve(quiet, storage_path).await,
    }
}

async fn serve(quiet: bool, storage_path: Option<PathBuf>) {
    let _lease = match storage_path {
        Some(path) => match acquire_write_lease(&path) {
            Ok(lease) => Some(lease),
            Err(error) => {
                eprintln!("steerable-sidecar: {error}");
                std::process::exit(1);
            }
        },
        None => None,
    };
    if !quiet {
        eprint!("{}", ready_marker());
        let _ = io::stderr().flush();
    }

    let mut stdout = tokio::io::stdout();
    let ready_note = notification(
        "lifecycle.ready",
        json!({
            "version": SIDECAR_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
            "pid": std::process::id(),
            "listenInfo": {"transport": "stdio", "engine": "rust"},
        }),
    );
    if stdout.write_all(&encode_frame(&ready_note)).await.is_err() {
        return;
    }
    let _ = stdout.flush().await;

    let mut state = SidecarState::new();
    let mut stdin = BufReader::new(tokio::io::stdin());
    let mut line = String::new();
    loop {
        line.clear();
        match stdin.read_line(&mut line).await {
            Ok(0) => break,
            Ok(_) => {}
            Err(_) => break,
        }
        let Some(request) = steerable_sidecar::rpc::decode_frame(&line) else {
            continue;
        };
        if request.get("id").is_none() {
            continue;
        }
        match dispatch(&mut state, &request) {
            Dispatch::Reply(reply) => {
                if stdout.write_all(&encode_frame(&reply)).await.is_err() {
                    break;
                }
                let _ = stdout.flush().await;
            }
            Dispatch::Async(reply) => {
                let reply = reply.await;
                if stdout.write_all(&encode_frame(&reply)).await.is_err() {
                    break;
                }
                let _ = stdout.flush().await;
            }
            Dispatch::Stream {
                reply,
                stream_id,
                messages,
                llm,
            } => {
                if stdout.write_all(&encode_frame(&reply)).await.is_err() {
                    break;
                }
                let _ = stdout.flush().await;
                let notes = run_turn(messages, stream_id, llm).await;
                for note in notes {
                    if stdout.write_all(&encode_frame(&note)).await.is_err() {
                        return;
                    }
                }
                let _ = stdout.flush().await;
            }
        }
        if state.shutdown {
            break;
        }
    }
}
