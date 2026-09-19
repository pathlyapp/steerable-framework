use serde_json::{json, Value};
use steerable_sidecar::file_edit::{apply_edits, apply_edits_rpc, EditOp};
use steerable_sidecar::methods::{dispatch, Dispatch, SidecarState};

fn applied(content: &str, edits: Vec<(&str, &str)>) -> steerable_sidecar::file_edit::ApplyResult {
    let ops: Vec<EditOp> = edits
        .into_iter()
        .map(|(old, new)| EditOp {
            old_text: old.to_string(),
            new_text: new.to_string(),
        })
        .collect();
    apply_edits(content, &ops, "file").expect("apply")
}

fn rpc(params: Value) -> Value {
    let mut state = SidecarState::new();
    match dispatch(
        &mut state,
        &json!({"jsonrpc":"2.0","id":1,"method":"workspace.apply_edits","params": params}),
    ) {
        Dispatch::Reply(value) => value,
        _ => panic!("expected reply"),
    }
}

#[test]
fn exact_single() {
    let out = applied("hello world\nfoo bar\n", vec![("foo bar", "baz qux")]);
    assert_eq!(out.content, "hello world\nbaz qux\n");
    assert_eq!(out.matches[0].level, "exact");
}

#[test]
fn multiple_reverse_order() {
    let out = applied(
        "a = 1\nb = 2\nc = 3\n",
        vec![("a = 1", "a = 10"), ("c = 3", "c = 30")],
    );
    assert_eq!(out.content, "a = 10\nb = 2\nc = 30\n");
}

#[test]
fn trim_level_multiline() {
    let src = "function f() {\n    if (x) {\n        return 1;\n    }\n}\n";
    let out = applied(
        src,
        vec![("if (x) {\nreturn 1;\n}", "if (x) {\n  return 2;\n}")],
    );
    assert_eq!(out.matches[0].level, "trim");
    assert_eq!(out.content, "function f() {\nif (x) {\n  return 2;\n}\n}\n");
}

#[test]
fn unicode_level() {
    let out = applied("const s = \"hello\";\n", vec![("“hello”", "\"bye\"")]);
    assert_eq!(out.content, "const s = \"bye\";\n");
    assert_eq!(out.matches[0].level, "unicode");
}

#[test]
fn not_found_ambiguous_overlap_empty() {
    assert_eq!(
        apply_edits(
            "abc\n",
            &[EditOp {
                old_text: "xyz".into(),
                new_text: "q".into()
            }],
            "file"
        )
        .unwrap_err()
        .code,
        "not_found"
    );
    assert_eq!(
        apply_edits(
            "foo\nfoo\n",
            &[EditOp {
                old_text: "foo".into(),
                new_text: "bar".into()
            }],
            "file"
        )
        .unwrap_err()
        .code,
        "ambiguous"
    );
    assert_eq!(
        apply_edits(
            "abcdef\n",
            &[
                EditOp {
                    old_text: "abc".into(),
                    new_text: "X".into()
                },
                EditOp {
                    old_text: "cde".into(),
                    new_text: "Y".into()
                }
            ],
            "file"
        )
        .unwrap_err()
        .code,
        "overlap"
    );
    assert_eq!(
        apply_edits(
            "abc",
            &[EditOp {
                old_text: String::new(),
                new_text: "x".into()
            }],
            "file"
        )
        .unwrap_err()
        .code,
        "empty_old"
    );
    assert_eq!(
        apply_edits("abc", &[], "file").unwrap_err().code,
        "no_edits"
    );
}

#[test]
fn diff_hunks_split_and_merge() {
    let far = (0..30)
        .map(|i| format!("l{i}"))
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    let ops = vec![
        EditOp {
            old_text: "l2".into(),
            new_text: "L2".into(),
        },
        EditOp {
            old_text: "l25".into(),
            new_text: "L25".into(),
        },
    ];
    let out = apply_edits(&far, &ops, "f.txt").unwrap();
    assert_eq!(out.diff.matches("@@ ").count(), 2);
    let near = applied("a\nb\nc\nd\n", vec![("a", "A"), ("b", "B")]);
    assert_eq!(near.diff.matches("@@ ").count(), 1);
    assert!(near.diff.contains("-a"));
    assert!(near.diff.contains("+A"));
}

#[test]
fn rpc_returns_content_diff_and_matches() {
    let response = rpc(json!({
        "content": "alpha\nbeta\ngamma\n",
        "filePath": "note.txt",
        "edits": [{"oldText": "beta", "newText": "BETA"}],
    }));
    let result = &response["result"];
    assert_eq!(result["content"], json!("alpha\nBETA\ngamma\n"));
    assert_eq!(result["applied"], json!(1));
    assert_eq!(
        result["matches"],
        json!([{"level": "exact", "startLine": 1, "oldLineCount": 1}])
    );
    let diff = result["diff"].as_str().unwrap();
    assert!(diff.contains("--- a/note.txt"));
    assert!(diff.contains("-beta"));
    assert!(diff.contains("+BETA"));
}

#[test]
fn rpc_batches_in_reverse_order() {
    let response = rpc(json!({
        "content": "one\ntwo\nthree\n",
        "edits": [
            {"oldText": "one", "newText": "1"},
            {"oldText": "three", "newText": "3"},
        ],
    }));
    assert_eq!(response["result"]["content"], json!("1\ntwo\n3\n"));
    assert_eq!(response["result"]["applied"], json!(2));
}

#[test]
fn rpc_edit_failure_carries_code() {
    let response = rpc(json!({
        "content": "alpha\n",
        "edits": [{"oldText": "missing", "newText": "x"}],
    }));
    assert_eq!(response["error"]["kind"], json!("edit_failed"));
    assert_eq!(response["error"]["data"]["code"], json!("not_found"));
}

#[test]
fn rpc_requires_content_and_edits() {
    let missing_content = rpc(json!({"edits": [{"oldText": "a", "newText": "b"}]}));
    assert_eq!(missing_content["error"]["kind"], json!("invalid_params"));
    let missing_edits = rpc(json!({"content": "a\n"}));
    assert_eq!(missing_edits["error"]["kind"], json!("invalid_params"));
}

#[test]
fn apply_edits_rpc_same_as_dispatch() {
    let ok = apply_edits_rpc(&json!({
        "content": "x\n",
        "edits": [{"oldText": "x", "newText": "y"}],
    }))
    .unwrap();
    assert_eq!(ok["content"], json!("y\n"));
}
