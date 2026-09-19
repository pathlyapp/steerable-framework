//! Newline-delimited JSON-RPC 2.0 frames (Python `stdio_jsonrpc.encode_frame`).

use serde_json::{json, Map, Value};

pub fn encode_frame(payload: &Value) -> Vec<u8> {
    let mut out = serde_json::to_vec(payload).expect("json-rpc frame is serializable");
    out.push(b'\n');
    out
}

pub fn decode_frame(line: &str) -> Option<Value> {
    let line = line.trim();
    if line.is_empty() {
        return None;
    }
    serde_json::from_str(line).ok()
}

pub fn response_ok(id: &Value, result: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": result})
}

pub fn response_null(id: &Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": Value::Null})
}

pub fn response_error(id: &Value, code: i64, message: &str, kind: &str) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": code, "message": message, "kind": kind}
    })
}

pub fn response_error_data(id: &Value, code: i64, message: &str, kind: &str, data: Value) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": code, "message": message, "kind": kind, "data": data}
    })
}

pub fn notification(method: &str, params: Value) -> Value {
    json!({"jsonrpc": "2.0", "method": method, "params": params})
}

pub fn object_params(value: &Value) -> Map<String, Value> {
    value.as_object().cloned().unwrap_or_default()
}
