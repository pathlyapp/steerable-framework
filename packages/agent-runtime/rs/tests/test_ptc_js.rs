use std::collections::HashMap;

use pretty_assertions::assert_eq;
use serde_json::json;
use steerable_agent_runtime::{
    invoke_run_js, invoke_wait_js, nested_ptc_refused, ptc_js_enabled, run_js_tool_descriptor,
    wait_js_tool_descriptor, worker_environ,
};

#[test]
fn ptc_js_disabled_by_default() {
    assert!(!ptc_js_enabled(&HashMap::new()));
    let mut env = HashMap::new();
    env.insert("STEERABLE_PTC_JS".into(), "1".into());
    assert!(ptc_js_enabled(&env));
}

#[test]
fn tool_descriptor_shapes() {
    let run = run_js_tool_descriptor();
    assert_eq!(run["type"], "function");
    assert_eq!(run["function"]["name"], "run_js");
    assert_eq!(run["function"]["parameters"]["required"], json!(["code"]));
    let wait = wait_js_tool_descriptor();
    assert_eq!(wait["function"]["name"], "wait_js");
    assert_eq!(
        wait["function"]["parameters"]["required"],
        json!(["cellId"])
    );
}

#[test]
fn worker_environ_is_an_allowlist() {
    let mut parent = HashMap::new();
    parent.insert("PATH".into(), "/usr/bin".into());
    parent.insert("HOME".into(), "/home/u".into());
    parent.insert("TMPDIR".into(), "/tmp".into());
    parent.insert("LANG".into(), "en_US.UTF-8".into());
    parent.insert("LC_ALL".into(), "en_US.UTF-8".into());
    parent.insert("STEERABLE_API_KEY".into(), "sk-secret".into());
    parent.insert("OPENAI_API_KEY".into(), "oa-secret".into());
    parent.insert("STEERABLE_PTC_JS".into(), "1".into());
    parent.insert("ELECTRON_RUN_AS_NODE".into(), "1".into());
    parent.insert("STEERABLE_PTC_JS_SESSION_TTL_MS".into(), "60000".into());
    parent.insert("NODE_OPTIONS".into(), "--require /tmp/evil.js".into());
    let child = worker_environ(&parent);
    assert_eq!(child.get("PATH").map(String::as_str), Some("/usr/bin"));
    assert_eq!(child.get("LC_ALL").map(String::as_str), Some("en_US.UTF-8"));
    assert_eq!(
        child.get("ELECTRON_RUN_AS_NODE").map(String::as_str),
        Some("1")
    );
    assert_eq!(
        child
            .get("STEERABLE_PTC_JS_SESSION_TTL_MS")
            .map(String::as_str),
        Some("60000")
    );
    for leaked in [
        "STEERABLE_API_KEY",
        "OPENAI_API_KEY",
        "STEERABLE_PTC_JS",
        "NODE_OPTIONS",
    ] {
        assert!(!child.contains_key(leaked), "{leaked} leaked");
    }
}

#[test]
fn missing_node_is_node_unavailable() {
    let mut env = HashMap::new();
    env.insert("PATH".into(), String::new());
    let result = invoke_run_js(&json!({"code": "return 1"}), &env, true);
    assert!(!result.success);
    assert_eq!(result.error.as_deref(), Some("node_unavailable"));
}

#[test]
fn explicit_node_path_must_exist() {
    let mut env = HashMap::new();
    env.insert("STEERABLE_PTC_NODE".into(), "/nonexistent/node-bin".into());
    let result = invoke_run_js(&json!({"code": "return 1"}), &env, true);
    assert_eq!(result.error.as_deref(), Some("node_unavailable"));
    assert!(result.data.unwrap()["message"]
        .as_str()
        .unwrap()
        .contains("/nonexistent/node-bin"));
}

#[test]
fn no_backend_is_sandbox_unavailable() {
    let mut env = HashMap::new();
    if let Some(node) = which_node_if_any() {
        env.insert("STEERABLE_PTC_NODE".into(), node);
    } else {
        env.insert(
            "STEERABLE_PTC_NODE".into(),
            std::env::current_exe()
                .unwrap()
                .to_string_lossy()
                .into_owned(),
        );
    }
    let result = invoke_run_js(&json!({"code": "return 1"}), &env, false);
    assert_eq!(result.error.as_deref(), Some("sandbox_unavailable"));
}

#[test]
fn nested_run_js_and_wait_js_are_refused() {
    let denied = nested_ptc_refused("run_js").unwrap();
    assert!(denied.error.unwrap().contains("nested run_js"));
    assert!(nested_ptc_refused("wait_js")
        .unwrap()
        .error
        .unwrap()
        .contains("nested wait_js"));
    assert!(nested_ptc_refused("bash").is_none());
}

#[test]
fn empty_code_and_source_cap() {
    let env = HashMap::new();
    let empty = invoke_run_js(&json!({"code": "  "}), &env, true);
    assert_eq!(empty.error.as_deref(), Some("code is empty"));
    let huge = "x".repeat(100_001);
    let capped = invoke_run_js(&json!({"code": huge}), &env, true);
    assert_eq!(
        capped.error.as_deref(),
        Some("run_js source exceeds the size cap")
    );
}

#[test]
fn wait_js_requires_cell_id() {
    let result = invoke_wait_js(&json!({}));
    assert!(!result.success);
    assert!(result.error.unwrap().contains("cellId"));
}

fn which_node_if_any() -> Option<String> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join("node");
        if candidate.is_file() {
            return Some(candidate.to_string_lossy().into_owned());
        }
    }
    None
}
