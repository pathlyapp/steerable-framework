use std::collections::HashMap;

use pretty_assertions::assert_eq;
use serde_json::json;
use steerable_agent_runtime::{
    child_environ, drive_scripted, refuse_nested, run_code_enabled, sandbox_unavailable,
    source_cap_error, ToolResult, MAX_NESTED_CALLS, MAX_SOURCE_BYTES, RUN_CODE,
};

#[test]
fn run_code_disabled_by_default() {
    assert!(!run_code_enabled(&HashMap::new()));
}

#[test]
fn run_code_enabled_by_flag() {
    let mut env = HashMap::new();
    env.insert("STEERABLE_RUN_CODE".into(), "1".into());
    assert!(run_code_enabled(&env));
}

#[test]
fn child_environ_scrubs_credentials() {
    let mut env = HashMap::new();
    env.insert("PATH".into(), "/bin".into());
    env.insert("STEERABLE_API_KEY".into(), "secret".into());
    env.insert("OPENAI_API_KEY".into(), "sk".into());
    let child = child_environ(&env);
    assert_eq!(child.get("PATH").map(String::as_str), Some("/bin"));
    assert!(!child.contains_key("STEERABLE_API_KEY"));
    assert!(!child.contains_key("OPENAI_API_KEY"));
    assert_eq!(
        child.get("PYTHONDONTWRITEBYTECODE").map(String::as_str),
        Some("1")
    );
}

#[test]
fn nested_run_code_is_refused() {
    let denied = refuse_nested("run_code", &[RUN_CODE]).unwrap();
    assert!(!denied.success);
    assert!(!denied.needs_followup);
    assert!(denied.error.unwrap().contains("nested run_code"));
    assert!(refuse_nested("bash", &[RUN_CODE]).is_none());
}

#[test]
fn source_cap_rejects_oversized_program() {
    let huge = "x".repeat(MAX_SOURCE_BYTES + 1);
    let error = source_cap_error(&huge).unwrap();
    assert!(error.error.unwrap().contains("size cap"));
    assert!(source_cap_error("return 1").is_none());
}

#[test]
fn no_backend_is_sandbox_unavailable() {
    let result = sandbox_unavailable();
    assert_eq!(result.error.as_deref(), Some("sandbox_unavailable"));
    assert!(!result.needs_followup);
    assert_eq!(result.data.unwrap()["_sandbox"]["backend"], "none");
}

#[test]
fn pump_two_stub_tools_then_done() {
    let inbound = vec![
        json!({"type": "log", "text": "hi"}),
        json!({"type": "call", "id": 1, "tool": "stub_a", "arguments": {}}),
        json!({"type": "call", "id": 2, "tool": "stub_b", "arguments": {"n": 1}}),
        json!({"type": "done", "ok": true, "value": {"a": 1, "b": 1}}),
    ];
    let (result, replies) = drive_scripted(
        "two stubs",
        json!({"backend": "test", "enforcement": "full"}),
        &inbound,
        |tool, _args| ToolResult::ok(json!({"who": tool})),
    );
    assert!(result.success, "{:?}", result.error);
    let data = result.data.unwrap();
    assert_eq!(data["logs"], json!(["hi"]));
    assert_eq!(data["calls"].as_array().unwrap().len(), 2);
    assert_eq!(data["_sandbox"]["backend"], "test");
    assert_eq!(replies.len(), 2);
    assert_eq!(replies[0]["ok"], true);
}

#[test]
fn pump_refuses_nested_run_code_frame() {
    let inbound = vec![
        json!({"type": "call", "id": 1, "tool": "run_code", "arguments": {"code": "return 1"}}),
        json!({"type": "done", "ok": true, "value": null}),
    ];
    let (result, replies) = drive_scripted("nested", json!({}), &inbound, |_tool, _args| {
        panic!("must not dispatch nested run_code")
    });
    assert!(result.success);
    assert_eq!(replies[0]["ok"], false);
    assert!(replies[0]["error"].as_str().unwrap().contains("nested"));
}

#[test]
fn pump_caps_nested_calls() {
    let mut inbound = Vec::new();
    for i in 0..=MAX_NESTED_CALLS {
        inbound.push(json!({"type": "call", "id": i, "tool": "stub", "arguments": {}}));
    }
    let (result, _) = drive_scripted("cap", json!({}), &inbound, |_tool, _args| {
        ToolResult::ok(json!({}))
    });
    assert!(!result.success);
    assert!(result
        .error
        .unwrap()
        .contains(&format!("exceeded {MAX_NESTED_CALLS}")));
}
