//! SKILL.md listing for `skills.list` (Python `steerable_agent_runtime.skills`).

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

pub const EAGER_PRIORITY_THRESHOLD: i64 = 850;
const NAME_MAX_LEN: usize = 64;
const DESC_MAX_LEN: usize = 1024;
pub const DEFAULT_MAX_CATALOG_SKILLS: usize = 50;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SkillDefinition {
    pub name: String,
    pub description: String,
    pub display_name: String,
    pub priority: i64,
    pub layer: String,
    pub model_invocable: bool,
    pub conditions: Vec<String>,
    pub match_mode: String,
    pub dir_name: String,
    pub content: String,
    pub root: String,
    pub tags: Vec<String>,
}

impl SkillDefinition {
    pub fn to_rpc(&self) -> Value {
        json!({
            "name": self.name,
            "displayName": self.display_name,
            "description": self.description,
            "priority": self.priority,
            "tags": self.tags,
            "conditions": self.conditions,
            "match": self.match_mode,
            "layer": self.layer,
            "modelInvocable": self.model_invocable,
            "content": self.content,
            "dirName": self.dir_name,
            "skillsDir": self.root,
        })
    }
}

pub fn matches_conditions(skill: &SkillDefinition, active: &[String]) -> bool {
    if skill.conditions.is_empty() {
        return true;
    }
    if skill.match_mode == "all" {
        skill
            .conditions
            .iter()
            .all(|condition| active.iter().any(|item| item == condition))
    } else {
        skill
            .conditions
            .iter()
            .any(|condition| active.iter().any(|item| item == condition))
    }
}

fn is_excluded(skill: &SkillDefinition, excluded: &[String]) -> bool {
    let names = [
        skill.name.to_ascii_lowercase(),
        skill.dir_name.to_ascii_lowercase(),
        skill.display_name.to_ascii_lowercase(),
    ];
    excluded
        .iter()
        .any(|item| names.iter().any(|name| !name.is_empty() && name == item))
}

pub fn select_skills(
    skills: &[SkillDefinition],
    conditions: &[String],
    exclude: &[String],
    ignore_conditions: bool,
) -> Vec<SkillDefinition> {
    let excluded: Vec<String> = exclude
        .iter()
        .map(|item| item.trim().to_ascii_lowercase())
        .collect();
    skills
        .iter()
        .filter(|skill| {
            !is_excluded(skill, &excluded)
                && (ignore_conditions || matches_conditions(skill, conditions))
        })
        .cloned()
        .collect()
}

pub fn select_catalog(
    skills: &[SkillDefinition],
    conditions: &[String],
    exclude: &[String],
    ignore_conditions: bool,
) -> Vec<SkillDefinition> {
    select_skills(skills, conditions, exclude, ignore_conditions)
        .into_iter()
        .filter(|skill| skill.layer == "catalog" && skill.model_invocable)
        .collect()
}

pub fn render_skill_catalog(
    skills: &[SkillDefinition],
    tool_name: &str,
    max_skills: usize,
) -> String {
    let mut lines = vec![
        "# Available skills (load on demand)".to_string(),
        String::new(),
        format!(
            "The skills below may be relevant to this turn; their full instructions are not loaded yet. When the task matches one, call the `{tool_name}` tool with its `name` to load the instructions, then follow them. If none match, ignore this list — do not guess unlisted skill names."
        ),
        String::new(),
    ];
    let mut ranked = skills.to_vec();
    ranked.sort_by_key(|skill| std::cmp::Reverse(skill.priority));
    let shown = ranked.iter().take(max_skills).collect::<Vec<_>>();
    for skill in &shown {
        let label = if skill.display_name.is_empty() {
            skill.name.clone()
        } else {
            format!("{}({})", skill.name, skill.display_name)
        };
        if skill.description.is_empty() {
            lines.push(format!("- {label}"));
        } else {
            lines.push(format!("- {label}: {}", skill.description));
        }
    }
    let omitted = ranked.len().saturating_sub(shown.len());
    if omitted > 0 {
        lines.push(format!(
            "- …and {omitted} more skills not listed (catalog capped at {max_skills}); ask if none of these fit."
        ));
    }
    lines.join("\n")
}

pub struct FilesystemSkillProvider {
    skills: Vec<SkillDefinition>,
}

impl FilesystemSkillProvider {
    pub fn new(roots: &[PathBuf]) -> Self {
        let mut merged: std::collections::HashMap<String, SkillDefinition> =
            std::collections::HashMap::new();
        for root in roots {
            for definition in load_root(root) {
                merged.insert(definition.name.to_ascii_lowercase(), definition);
            }
        }
        let mut skills: Vec<SkillDefinition> = merged.into_values().collect();
        skills.sort_by(|a, b| a.dir_name.cmp(&b.dir_name));
        Self { skills }
    }

    pub fn list(&self) -> &[SkillDefinition] {
        &self.skills
    }

    pub fn get(&self, name: &str) -> Option<&SkillDefinition> {
        let key = name.trim().to_ascii_lowercase();
        self.skills.iter().find(|skill| {
            skill.name.to_ascii_lowercase() == key
                || skill.dir_name.to_ascii_lowercase() == key
                || (!skill.display_name.is_empty()
                    && skill.display_name.to_ascii_lowercase() == key)
        })
    }
}

fn load_root(root: &Path) -> Vec<SkillDefinition> {
    let Ok(entries) = fs::read_dir(root) else {
        return Vec::new();
    };
    let mut dirs: Vec<PathBuf> = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect();
    dirs.sort();
    dirs.into_iter()
        .filter_map(|path| parse_skill_dir(&path))
        .collect()
}

fn parse_skill_dir(skill_dir: &Path) -> Option<SkillDefinition> {
    let skill_file = skill_dir.join("SKILL.md");
    if !skill_file.is_file() {
        return None;
    }
    let raw = fs::read_to_string(&skill_file).ok()?;
    let (fm_raw, body) = split_frontmatter(&raw);
    let content = body.trim();
    if content.is_empty() {
        return None;
    }
    let fm = if fm_raw.trim().is_empty() {
        Map::new()
    } else {
        parse_frontmatter(&fm_raw)
    };
    let name = fm.get("name").and_then(Value::as_str).unwrap_or("");
    if name.is_empty() || !valid_name(name) {
        return None;
    }
    let description = fm
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let _ = description.len() > DESC_MAX_LEN;
    let priority = match fm.get("priority") {
        Some(Value::Number(number)) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|n| n as i64))
            .unwrap_or(500),
        _ => 500,
    };
    let conditions = string_list(fm.get("conditions"));
    let tags = string_list(fm.get("tags"));
    let match_mode = if fm
        .get("match")
        .and_then(Value::as_str)
        .unwrap_or("")
        .eq_ignore_ascii_case("all")
    {
        "all"
    } else {
        "any"
    }
    .to_string();
    let layer_raw = fm
        .get("layer")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase();
    let layer = if layer_raw == "eager" || layer_raw == "catalog" {
        layer_raw
    } else if priority >= EAGER_PRIORITY_THRESHOLD {
        "eager".into()
    } else {
        "catalog".into()
    };
    let model_invocable = fm.get("disable-model-invocation") != Some(&json!(true));
    let scripts_path = skill_dir
        .join("scripts")
        .to_string_lossy()
        .replace('\\', "/");
    let mut content = content.to_string();
    content = replace_scripts(&content, &scripts_path);
    let display_name = fm
        .get("displayName")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    Some(SkillDefinition {
        name: name.to_string(),
        description,
        display_name,
        priority,
        layer,
        model_invocable,
        conditions,
        match_mode,
        dir_name: skill_dir
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_default(),
        content,
        root: skill_dir
            .parent()
            .map(|path| path.to_string_lossy().into_owned())
            .unwrap_or_default(),
        tags,
    })
}

fn split_frontmatter(raw: &str) -> (String, String) {
    if !raw.starts_with("---") {
        return (String::new(), raw.to_string());
    }
    let rest = &raw[3..];
    let Some(end) = rest.find("\n---") else {
        return (String::new(), raw.to_string());
    };
    let fm_raw = rest[..end].to_string();
    let mut body = rest[end + 4..].to_string();
    if body.starts_with('\r') {
        body = body[1..].to_string();
    }
    if body.starts_with('\n') {
        body = body[1..].to_string();
    }
    (fm_raw, body)
}

fn replace_scripts(content: &str, scripts_path: &str) -> String {
    let mut out = String::new();
    let bytes = content.as_bytes();
    let mut index = 0;
    let needle = b"{scripts}";
    while index < bytes.len() {
        if let Some(rel) = content[index..].find("{scripts}") {
            out.push_str(&content[index..index + rel]);
            let after = index + rel + needle.len();
            if content
                .as_bytes()
                .get(after)
                .is_some_and(|ch| *ch == b'/' || *ch == b'\\')
            {
                out.push_str(scripts_path);
                out.push('/');
                index = after + 1;
            } else {
                out.push_str(scripts_path);
                index = after;
            }
        } else {
            out.push_str(&content[index..]);
            break;
        }
    }
    out
}

fn valid_name(name: &str) -> bool {
    if name.is_empty() || name.len() > NAME_MAX_LEN || name.contains("--") {
        return false;
    }
    let bytes = name.as_bytes();
    let first = bytes[0];
    if !first.is_ascii_lowercase() && !first.is_ascii_digit() {
        return false;
    }
    if bytes.len() == 1 {
        return true;
    }
    let last = bytes[bytes.len() - 1];
    if !last.is_ascii_lowercase() && !last.is_ascii_digit() {
        return false;
    }
    bytes[1..bytes.len() - 1]
        .iter()
        .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || *ch == b'-')
}

fn parse_frontmatter(raw: &str) -> Map<String, Value> {
    let mut out = Map::new();
    for raw_line in raw.split('\n') {
        let trimmed = raw_line.trim_end_matches('\r').trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let Some(colon) = trimmed.find(':') else {
            continue;
        };
        let key = trimmed[..colon].trim();
        let value = trimmed[colon + 1..].trim();
        if value.is_empty() {
            continue;
        }
        out.insert(key.to_string(), parse_value(value));
    }
    out
}

fn parse_value(value: &str) -> Value {
    if value.starts_with('[') && value.ends_with(']') {
        let inner = value[1..value.len() - 1].trim();
        if inner.is_empty() {
            return json!([]);
        }
        let items: Vec<Value> = inner
            .split(',')
            .map(str::trim)
            .map(unquote)
            .filter(|item| !item.is_empty())
            .map(Value::from)
            .collect();
        return Value::Array(items);
    }
    if (value.starts_with('"') && value.ends_with('"'))
        || (value.starts_with('\'') && value.ends_with('\''))
    {
        return Value::from(unquote(value));
    }
    if value == "true" {
        return json!(true);
    }
    if value == "false" {
        return json!(false);
    }
    if value
        .chars()
        .all(|ch| ch.is_ascii_digit() || ch == '-' || ch == '.')
        && value.chars().any(|ch| ch.is_ascii_digit())
    {
        if value.contains('.') {
            if let Ok(number) = value.parse::<f64>() {
                return json!(number);
            }
        } else if let Ok(number) = value.parse::<i64>() {
            return json!(number);
        }
    }
    Value::from(value)
}

fn unquote(value: &str) -> String {
    if value.len() >= 2 {
        let bytes = value.as_bytes();
        if bytes[0] == bytes[value.len() - 1] && (bytes[0] == b'\'' || bytes[0] == b'"') {
            return value[1..value.len() - 1].to_string();
        }
    }
    value.to_string()
}

fn string_list(value: Option<&Value>) -> Vec<String> {
    match value {
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect(),
        _ => Vec::new(),
    }
}

pub fn list_skills_rpc(params: &Value) -> Result<Value, (i64, String, &'static str)> {
    let Some(roots_raw) = params.get("roots").and_then(Value::as_array) else {
        return Err((
            -32602,
            "skills.list: `roots` (array of paths) is required".into(),
            "invalid_params",
        ));
    };
    let roots: Vec<PathBuf> = roots_raw
        .iter()
        .filter_map(Value::as_str)
        .map(PathBuf::from)
        .collect();
    let conditions: Vec<String> = params
        .get("conditions")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let exclude: Vec<String> = params
        .get("exclude")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let ignore_conditions = params
        .get("ignoreConditions")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let provider = FilesystemSkillProvider::new(&roots);
    let selected = select_skills(provider.list(), &conditions, &exclude, ignore_conditions);
    Ok(json!({
        "skills": selected.iter().map(SkillDefinition::to_rpc).collect::<Vec<_>>(),
    }))
}
