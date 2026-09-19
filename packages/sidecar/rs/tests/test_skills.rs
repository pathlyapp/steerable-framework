use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::json;
use steerable_agent_runtime::{
    list_skills_rpc, select_catalog, FilesystemSkillProvider, SkillDefinition,
};
use steerable_sidecar::methods::{dispatch, Dispatch, SidecarState};

struct TmpDir {
    path: PathBuf,
}

impl TmpDir {
    fn new(label: &str) -> Self {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "steerable-skills-{label}-{}-{nanos}",
            std::process::id()
        ));
        fs::create_dir_all(&path).unwrap();
        Self { path }
    }
}

impl Drop for TmpDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

fn write_skill(root: &Path, dir_name: &str, text: &str) {
    let skill_dir = root.join(dir_name);
    fs::create_dir_all(&skill_dir).unwrap();
    fs::write(skill_dir.join("SKILL.md"), text).unwrap();
}

fn fixture_root() -> TmpDir {
    let dir = TmpDir::new("root");
    let root = dir.path.join("skills");
    write_skill(
        &root,
        "00-identity",
        "---\nname: identity\ndescription: Defines the assistant core role. Always loaded as the foundational skill.\npriority: 1000\n---\n\n# 角色\n你是测试助手。\n",
    );
    write_skill(
        &root,
        "81-anti-deferred",
        "---\nname: anti-deferred-execution\ndescription: 零容忍规则——光说不做即违规。\npriority: 880\nconditions: [has-tools]\n---\n\n# 反拖延\n",
    );
    write_skill(
        &root,
        "85-local-exec",
        "---\nname: local-exec\ndescription: Local shell / filesystem control via local_exec_shell.\npriority: 700\nconditions: [tool:local_exec_shell, tool:local_read_file]\nmatch: any\n---\n\n# 本地执行\n运行 `{scripts}/helper.py` 完成辅助任务。\n",
    );
    write_skill(
        &root,
        "90-csv-tools",
        "---\nname: csv-tools\ndisplayName: CSV 处理链\ndescription: \"CSV workflow guidance — loading, filtering, reporting.\"\npriority: 600\nconditions: [tool:csv_list_rows]\nmatch: all\n---\n\n# CSV Tools\n正文内容。\n",
    );
    dir
}

fn rpc(params: serde_json::Value) -> serde_json::Value {
    let mut state = SidecarState::new();
    match dispatch(
        &mut state,
        &json!({"jsonrpc":"2.0","id":1,"method":"skills.list","params": params}),
    ) {
        Dispatch::Reply(value) => value,
        _ => panic!("expected reply"),
    }
}

fn by_name(
    provider: &FilesystemSkillProvider,
) -> std::collections::HashMap<String, SkillDefinition> {
    provider
        .list()
        .iter()
        .cloned()
        .map(|skill| (skill.name.clone(), skill))
        .collect()
}

#[test]
fn frontmatter_field_compat() {
    let dir = fixture_root();
    let provider = FilesystemSkillProvider::new(&[dir.path.join("skills")]);
    let skills = by_name(&provider);
    assert_eq!(
        skills
            .keys()
            .cloned()
            .collect::<std::collections::BTreeSet<_>>(),
        [
            "anti-deferred-execution",
            "csv-tools",
            "identity",
            "local-exec"
        ]
        .into_iter()
        .map(str::to_string)
        .collect()
    );
    let csv = &skills["csv-tools"];
    assert_eq!(csv.display_name, "CSV 处理链");
    assert_eq!(
        csv.description,
        "CSV workflow guidance — loading, filtering, reporting."
    );
    assert_eq!(csv.priority, 600);
    assert_eq!(csv.conditions, vec!["tool:csv_list_rows".to_string()]);
    assert_eq!(csv.match_mode, "all");
    assert_eq!(csv.dir_name, "90-csv-tools");
    assert_eq!(skills["anti-deferred-execution"].match_mode, "any");
    assert_eq!(skills["identity"].layer, "eager");
    assert_eq!(skills["local-exec"].layer, "catalog");
}

#[test]
fn scripts_placeholder_and_aliases() {
    let dir = fixture_root();
    let root = dir.path.join("skills");
    let provider = FilesystemSkillProvider::new(&[root.clone()]);
    let skill = provider.get("local-exec").unwrap();
    assert!(!skill.content.contains("{scripts}"));
    assert!(skill.content.contains(
        &root
            .join("85-local-exec")
            .join("scripts")
            .to_string_lossy()
            .replace('\\', "/")
    ));
    assert!(provider.get("CSV-TOOLS").is_some());
    assert!(provider.get("90-csv-tools").is_some());
    assert!(provider.get("CSV 处理链").is_some());
}

#[test]
fn user_root_overrides_builtin() {
    let dir = TmpDir::new("override");
    let builtin = dir.path.join("builtin");
    let user = dir.path.join("user");
    write_skill(
        &builtin,
        "10-foo",
        "---\nname: foo\ndescription: builtin\n---\n\nbuiltin body\n",
    );
    write_skill(
        &user,
        "99-foo",
        "---\nname: foo\ndescription: user override\n---\n\nuser body\n",
    );
    let provider = FilesystemSkillProvider::new(&[builtin, user]);
    assert_eq!(provider.list().len(), 1);
    assert_eq!(provider.list()[0].description, "user override");
    assert_eq!(provider.get("foo").unwrap().content, "user body");
}

#[test]
fn name_validation() {
    let dir = TmpDir::new("names");
    let root = dir.path.join("skills");
    write_skill(&root, "bad-upper", "---\nname: BadName\n---\n\nbody\n");
    write_skill(&root, "bad-hyphens", "---\nname: bad--name\n---\n\nbody\n");
    write_skill(
        &root,
        "no-name",
        "---\ndescription: no name field\n---\n\nbody\n",
    );
    write_skill(&root, "good", "---\nname: good-name\n---\n\nbody\n");
    let provider = FilesystemSkillProvider::new(&[root]);
    assert_eq!(
        provider
            .list()
            .iter()
            .map(|s| s.name.as_str())
            .collect::<Vec<_>>(),
        vec!["good-name"]
    );
}

#[test]
fn rpc_parses_layers_tags_and_conditions() {
    let dir = TmpDir::new("rpc");
    let root = dir.path.join("skills");
    write_skill(
        &root,
        "00-identity",
        "---\nname: identity\ndescription: Who am I\npriority: 1000\ntags: [base, core]\n---\n\n# Identity\nAlways eager.\n",
    );
    write_skill(
        &root,
        "85-local-exec",
        "---\nname: local-exec\ndescription: Run shell\npriority: 700\nconditions: [tool:local_exec_shell]\n---\n\n# Local exec\nCatalog body.\n",
    );
    write_skill(
        &root,
        "90-csv-tools",
        "---\nname: csv-tools\ndescription: y\npriority: 600\n---\n\nbody\n",
    );
    let listed = rpc(json!({"roots": [root.to_string_lossy()]}));
    let skills = &listed["result"]["skills"];
    let identity = skills
        .as_array()
        .unwrap()
        .iter()
        .find(|skill| skill["name"] == "identity")
        .unwrap();
    assert_eq!(identity["layer"], "eager");
    assert_eq!(identity["tags"], json!(["base", "core"]));
    assert!(identity["content"]
        .as_str()
        .unwrap()
        .contains("Always eager."));
    assert_eq!(identity["dirName"], "00-identity");
    assert_eq!(identity["skillsDir"], root.to_string_lossy().as_ref());

    let gated = rpc(json!({"roots": [root.to_string_lossy()]}));
    let names: std::collections::BTreeSet<_> = gated["result"]["skills"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|skill| skill["name"].as_str())
        .collect();
    assert_eq!(names, ["csv-tools", "identity"].into_iter().collect());

    let matched = rpc(json!({
        "roots": [root.to_string_lossy()],
        "conditions": ["tool:local_exec_shell"],
    }));
    let names: std::collections::BTreeSet<_> = matched["result"]["skills"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|skill| skill["name"].as_str())
        .collect();
    assert_eq!(
        names,
        ["csv-tools", "identity", "local-exec"]
            .into_iter()
            .collect()
    );

    let excluded = rpc(json!({
        "roots": [root.to_string_lossy()],
        "conditions": ["tool:local_exec_shell"],
        "exclude": ["csv-tools"],
    }));
    let names: std::collections::BTreeSet<_> = excluded["result"]["skills"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|skill| skill["name"].as_str())
        .collect();
    assert_eq!(names, ["identity", "local-exec"].into_iter().collect());

    let missing = rpc(json!({}));
    assert_eq!(missing["error"]["kind"], "invalid_params");
}

#[test]
fn select_catalog_hides_eager_and_non_invocable() {
    let dir = fixture_root();
    let provider = FilesystemSkillProvider::new(&[dir.path.join("skills")]);
    assert!(select_catalog(provider.list(), &[], &[], false).is_empty());
    let catalog = select_catalog(provider.list(), &["tool:csv_list_rows".into()], &[], false);
    assert_eq!(
        catalog.iter().map(|s| s.name.as_str()).collect::<Vec<_>>(),
        vec!["csv-tools"]
    );
    let _ = list_skills_rpc(&json!({"roots": []})).unwrap();
}
