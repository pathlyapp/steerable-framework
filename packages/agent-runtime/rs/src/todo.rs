//! Session task list — `todo_write` (Python `todo.py`).

use std::collections::{HashMap, HashSet};

use serde_json::{json, Map, Value};

use crate::types::ToolResult;

pub const TODO_TOOL_NAME: &str = "todo_write";
pub const TODO_STATUSES: [&str; 3] = ["pending", "in_progress", "completed"];
pub const TODO_DESCRIPTION: &str = "Track multi-step work as an ordered task list. Create the list when a task needs several steps; rewrite it as you progress — mark the task you are working on in_progress (at most one) and completed the moment it is done. Each call replaces the whole list. Skip it for single-step requests.";
const MIN_TODOS: usize = 1;
const MAX_TODOS: usize = 50;

#[derive(Clone, Debug, Default)]
pub struct TodoStore {
    by_chat: HashMap<String, Vec<Value>>,
}

impl TodoStore {
    pub fn get(&self, chat_id: &str) -> Vec<Value> {
        self.by_chat.get(chat_id).cloned().unwrap_or_default()
    }

    pub fn set(&mut self, chat_id: &str, todos: Vec<Value>) {
        self.by_chat.insert(chat_id.to_string(), todos);
    }
}

pub fn apply_todo_write(
    store: &mut TodoStore,
    args: &Value,
    chat_id: &str,
) -> Result<Value, String> {
    let todos = args.get("todos").cloned().unwrap_or(Value::Null);
    let normalized = normalize_todos(&todos)?;
    store.set(chat_id, normalized.clone());
    let mut pending = 0;
    let mut in_progress = 0;
    let mut completed = 0;
    for item in &normalized {
        match item.get("status").and_then(Value::as_str) {
            Some("pending") => pending += 1,
            Some("in_progress") => in_progress += 1,
            Some("completed") => completed += 1,
            _ => {}
        }
    }
    Ok(json!({
        "todos": normalized,
        "summary": {
            "total": normalized.len(),
            "pending": pending,
            "inProgress": in_progress,
            "completed": completed,
        }
    }))
}

pub fn todo_write_result(store: &mut TodoStore, args: &Value, chat_id: &str) -> ToolResult {
    match apply_todo_write(store, args, chat_id) {
        Ok(data) => ToolResult::ok(data),
        Err(error) => ToolResult::fail(error),
    }
}

pub fn todo_write_tool_descriptor() -> Value {
    json!({
        "type": "function",
        "function": {
            "name": TODO_TOOL_NAME,
            "description": TODO_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "minItems": MIN_TODOS,
                        "maxItems": MAX_TODOS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Stable identifier for this task."},
                                "content": {"type": "string", "description": "One line describing the task."},
                                "status": {"type": "string", "enum": TODO_STATUSES},
                            },
                            "required": ["id", "content", "status"],
                        },
                    },
                },
                "required": ["todos"],
                "additionalProperties": false,
            },
        },
    })
}

fn normalize_todos(todos: &Value) -> Result<Vec<Value>, String> {
    let Some(items) = todos.as_array() else {
        return Err(format!(
            "todo_write: todos must be an array, got {}",
            json_type(todos)
        ));
    };
    if !(MIN_TODOS..=MAX_TODOS).contains(&items.len()) {
        return Err(format!(
            "todo_write: todos must contain {MIN_TODOS}-{MAX_TODOS} items, got {}. Track the current working set, not a backlog.",
            items.len()
        ));
    }
    let mut seen_ids = HashSet::new();
    let mut in_progress = 0;
    let mut normalized = Vec::new();
    for (index, item) in items.iter().enumerate() {
        let Some(obj) = item.as_object() else {
            return Err(format!(
                "todo_write: todos[{index}] must be an object, got {}",
                json_type(item)
            ));
        };
        let task_id = obj.get("id").and_then(Value::as_str).unwrap_or("");
        if task_id.is_empty() {
            return Err(format!(
                "todo_write: todos[{index}] is missing a non-empty string \"id\"."
            ));
        }
        if !seen_ids.insert(task_id.to_string()) {
            return Err(format!(
                "todo_write: duplicate id \"{task_id}\" — ids must be unique."
            ));
        }
        let content = obj.get("content").and_then(Value::as_str).unwrap_or("");
        if content.trim().is_empty() {
            return Err(format!(
                "todo_write: todos[{index}] (\"{task_id}\") is missing a non-empty string \"content\"."
            ));
        }
        let status = obj.get("status").and_then(Value::as_str).unwrap_or("");
        if !TODO_STATUSES.contains(&status) {
            return Err(format!(
                "todo_write: todos[{index}] (\"{task_id}\") has invalid status {status:?}; expected one of {}.",
                TODO_STATUSES.join(", ")
            ));
        }
        if status == "in_progress" {
            in_progress += 1;
        }
        let mut row = Map::new();
        row.insert("id".into(), json!(task_id));
        row.insert("content".into(), json!(content));
        row.insert("status".into(), json!(status));
        normalized.push(Value::Object(row));
    }
    if in_progress > 1 {
        return Err(format!(
            "todo_write: {in_progress} tasks are in_progress; keep at most one — mark the rest pending or completed."
        ));
    }
    Ok(normalized)
}

fn json_type(value: &Value) -> &'static str {
    match value {
        Value::Null => "None",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}
