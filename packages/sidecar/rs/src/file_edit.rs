//! Structured file editing (Python `steerable_sidecar.file_edit`).

use serde_json::{json, Value};

const UNICODE_PUNCT: &[(char, char)] = &[
    ('‘', '\''),
    ('’', '\''),
    ('‚', '\''),
    ('‛', '\''),
    ('“', '"'),
    ('”', '"'),
    ('„', '"'),
    ('‟', '"'),
    ('—', '-'),
    ('–', '-'),
    ('―', '-'),
    ('…', '.'),
    ('·', '.'),
    ('\u{00a0}', ' '),
];

#[derive(Debug)]
pub struct EditError {
    pub message: String,
    pub code: &'static str,
}

impl EditError {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            code,
        }
    }
}

#[derive(Clone, Debug)]
pub struct EditOp {
    pub old_text: String,
    pub new_text: String,
}

#[derive(Clone, Debug)]
pub struct AppliedEdit {
    pub index: usize,
    pub length: usize,
    pub level: &'static str,
    pub start_line: usize,
    pub old_line_count: usize,
    pub new_lines: Vec<String>,
}

#[derive(Debug)]
pub struct ApplyResult {
    pub content: String,
    pub matches: Vec<AppliedEdit>,
    pub diff: String,
}

fn normalise_unicode(text: &str) -> String {
    text.chars()
        .map(|ch| {
            UNICODE_PUNCT
                .iter()
                .find(|(from, _)| *from == ch)
                .map(|(_, to)| *to)
                .unwrap_or(ch)
        })
        .collect()
}

enum LocateHit {
    Found(usize, usize),
    Ambiguous,
}

fn find_from(haystack: &str, needle: &str) -> Option<LocateHit> {
    let first = haystack.find(needle)?;
    let next = first.saturating_add(1);
    if next <= haystack.len() && haystack[next..].contains(needle) {
        Some(LocateHit::Ambiguous)
    } else {
        Some(LocateHit::Found(first, needle.len()))
    }
}

fn find_exact(content: &str, old: &str) -> Option<LocateHit> {
    find_from(content, old)
}

fn find_trim(content: &str, old: &str) -> Option<LocateHit> {
    let old_lines: Vec<&str> = old.split('\n').map(str::trim).collect();
    if old_lines.is_empty() || (old_lines.len() == 1 && old_lines[0].is_empty()) {
        return None;
    }
    let mut line_starts = vec![0usize];
    for (i, ch) in content.char_indices() {
        if ch == '\n' {
            line_starts.push(i + 1);
        }
    }
    let line_count = line_starts.len();
    let line_end = |line: usize| -> usize {
        let start = line_starts[line];
        let nxt = if line + 1 < line_count {
            line_starts[line + 1]
        } else {
            content.len()
        };
        if nxt > start && content.as_bytes()[nxt - 1] == b'\n' {
            nxt - 1
        } else {
            nxt
        }
    };
    let trimmed: Vec<String> = (0..line_count)
        .map(|line| {
            content[line_starts[line]..line_end(line)]
                .trim()
                .to_string()
        })
        .collect();
    let window = old_lines.len();
    if window > line_count {
        return None;
    }
    let mut hits = Vec::new();
    for start in 0..=line_count - window {
        if trimmed[start..start + window]
            .iter()
            .map(String::as_str)
            .eq(old_lines.iter().copied())
        {
            hits.push(start);
        }
    }
    match hits.as_slice() {
        [] => None,
        [start_line] => {
            let index = line_starts[*start_line];
            let end = line_end(*start_line + window - 1);
            Some(LocateHit::Found(index, end - index))
        }
        _ => Some(LocateHit::Ambiguous),
    }
}

fn find_unicode(content: &str, old: &str) -> Option<LocateHit> {
    find_from(&normalise_unicode(content), &normalise_unicode(old))
}

fn locate(content: &str, old: &str) -> Result<(usize, usize, &'static str), EditError> {
    if old.is_empty() {
        return Err(EditError::new(
            "empty_old",
            "oldText 为空——edit 用于替换已有片段；新建/整文件覆盖请用 write_file。",
        ));
    }
    let levels: &[(&str, fn(&str, &str) -> Option<LocateHit>)] = &[
        ("exact", find_exact),
        ("trim", find_trim),
        ("unicode", find_unicode),
    ];
    let mut saw_ambiguous = false;
    for (level, run) in levels {
        match run(content, old) {
            Some(LocateHit::Ambiguous) => saw_ambiguous = true,
            Some(LocateHit::Found(index, length)) => return Ok((index, length, *level)),
            None => {}
        }
    }
    if saw_ambiguous {
        return Err(EditError::new(
            "ambiguous",
            "oldText 在文件中匹配到多处——请扩大上下文（多带几行）以唯一定位。",
        ));
    }
    Err(EditError::new(
        "not_found",
        "oldText 在文件中未找到（已尝试精确 / 去空白 / Unicode 归一三级匹配）。请先重新读取该文件，确认要替换的原文与当前内容一致。",
    ))
}

pub fn apply_edits(
    content: &str,
    edits: &[EditOp],
    file_path: &str,
) -> Result<ApplyResult, EditError> {
    if edits.is_empty() {
        return Err(EditError::new(
            "no_edits",
            "edits 为空——至少提供一条 {oldText, newText}。",
        ));
    }
    let mut matches = Vec::new();
    for edit in edits {
        let (index, length, level) = locate(content, &edit.old_text)?;
        let old_span = &content[index..index + length];
        matches.push(AppliedEdit {
            index,
            length,
            level,
            start_line: content[..index].matches('\n').count(),
            old_line_count: old_span.matches('\n').count() + 1,
            new_lines: edit.new_text.split('\n').map(str::to_string).collect(),
        });
    }
    let mut ordered = matches.clone();
    ordered.sort_by_key(|item| item.index);
    for i in 1..ordered.len() {
        let prev = &ordered[i - 1];
        let cur = &ordered[i];
        if cur.index < prev.index + prev.length {
            return Err(EditError::new(
                "overlap",
                format!(
                    "第 {} 条编辑与前面的编辑在原文中区间重叠——请合并成一条，或缩小各自的 oldText。",
                    i + 1
                ),
            ));
        }
    }
    let mut next_content = content.to_string();
    let mut pairs: Vec<(&AppliedEdit, &EditOp)> = matches.iter().zip(edits.iter()).collect();
    pairs.sort_by_key(|(item, _)| std::cmp::Reverse(item.index));
    for (item, edit) in pairs {
        next_content = format!(
            "{}{}{}",
            &next_content[..item.index],
            edit.new_text,
            &next_content[item.index + item.length..]
        );
    }
    let diff = build_unified_diff(file_path, content, &matches);
    Ok(ApplyResult {
        content: next_content,
        matches: ordered,
        diff,
    })
}

fn build_unified_diff(file_path: &str, original: &str, matches: &[AppliedEdit]) -> String {
    let orig_lines: Vec<&str> = original.split('\n').collect();
    let mut hunks = matches.to_vec();
    hunks.sort_by_key(|item| item.start_line);
    let context = 3usize;
    struct Group {
        start: usize,
        end: usize,
        items: Vec<AppliedEdit>,
    }
    let mut groups: Vec<Group> = Vec::new();
    for item in hunks {
        let hunk_end = item.start_line + item.old_line_count;
        if let Some(last) = groups.last_mut() {
            if item.start_line.saturating_sub(last.end) <= context * 2 {
                last.items.push(item);
                last.end = last.end.max(hunk_end);
                continue;
            }
        }
        groups.push(Group {
            start: item.start_line,
            end: hunk_end,
            items: vec![item],
        });
    }
    let mut out = vec![format!("--- a/{file_path}"), format!("+++ b/{file_path}")];
    let mut net_delta: isize = 0;
    for group in groups {
        let ctx_start = group.start.saturating_sub(context);
        let ctx_end = (group.end + context).min(orig_lines.len());
        let old_len = ctx_end - ctx_start;
        let removed: usize = group.items.iter().map(|item| item.old_line_count).sum();
        let added: usize = group.items.iter().map(|item| item.new_lines.len()).sum();
        let new_len = old_len - removed + added;
        let old_start = ctx_start + 1;
        let new_start = (ctx_start as isize + 1 + net_delta) as usize;
        out.push(format!(
            "@@ -{old_start},{old_len} +{new_start},{new_len} @@"
        ));
        let by_start: std::collections::HashMap<usize, AppliedEdit> = group
            .items
            .iter()
            .map(|item| (item.start_line, item.clone()))
            .collect();
        let mut i = ctx_start;
        while i < ctx_end {
            if let Some(hunk) = by_start.get(&i) {
                for line in &orig_lines[i..i + hunk.old_line_count] {
                    out.push(format!("-{line}"));
                }
                for line in &hunk.new_lines {
                    out.push(format!("+{line}"));
                }
                i += hunk.old_line_count;
            } else {
                out.push(format!(" {}", orig_lines[i]));
                i += 1;
            }
        }
        net_delta += added as isize - removed as isize;
    }
    out.join("\n")
}

pub fn apply_edits_rpc(params: &Value) -> Result<Value, (i64, String, &'static str, Value)> {
    let Some(content) = params.get("content").and_then(Value::as_str) else {
        return Err((
            -32602,
            "workspace.apply_edits: `content` (string) is required".into(),
            "invalid_params",
            Value::Null,
        ));
    };
    let Some(raw_edits) = params.get("edits").and_then(Value::as_array) else {
        return Err((
            -32602,
            "workspace.apply_edits: `edits` (array) is required".into(),
            "invalid_params",
            Value::Null,
        ));
    };
    let edits: Vec<EditOp> = raw_edits
        .iter()
        .filter_map(Value::as_object)
        .map(|entry| EditOp {
            old_text: entry
                .get("oldText")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            new_text: entry
                .get("newText")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        })
        .collect();
    let file_path = params
        .get("filePath")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or("file");
    match apply_edits(content, &edits, file_path) {
        Ok(result) => Ok(json!({
            "content": result.content,
            "diff": result.diff,
            "applied": result.matches.len(),
            "matches": result.matches.iter().map(|item| json!({
                "level": item.level,
                "startLine": item.start_line,
                "oldLineCount": item.old_line_count,
            })).collect::<Vec<_>>(),
        })),
        Err(error) => Err((
            -32030,
            error.message,
            "edit_failed",
            json!({"code": error.code}),
        )),
    }
}
