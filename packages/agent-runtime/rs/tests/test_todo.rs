use pretty_assertions::assert_eq;
use serde_json::json;
use steerable_agent_runtime::{apply_todo_write, TodoStore};

#[test]
fn todo_write_full_replace_and_summary() {
    let mut store = TodoStore::default();
    let out = apply_todo_write(
        &mut store,
        &json!({
            "todos": [
                {"id": "1", "content": "read", "status": "completed"},
                {"id": "2", "content": "write", "status": "in_progress"}
            ]
        }),
        "chat-a",
    )
    .unwrap();
    assert_eq!(out["summary"]["total"], json!(2));
    assert_eq!(out["summary"]["completed"], json!(1));
    assert_eq!(out["summary"]["inProgress"], json!(1));
    assert_eq!(store.get("chat-a").len(), 2);
    assert!(store.get("chat-b").is_empty());
}

#[test]
fn todo_write_rejects_two_in_progress() {
    let mut store = TodoStore::default();
    let err = apply_todo_write(
        &mut store,
        &json!({
            "todos": [
                {"id": "1", "content": "a", "status": "in_progress"},
                {"id": "2", "content": "b", "status": "in_progress"}
            ]
        }),
        "c",
    )
    .unwrap_err();
    assert!(err.contains("in_progress"));
}
