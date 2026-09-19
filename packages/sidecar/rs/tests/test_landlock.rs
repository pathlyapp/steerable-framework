use std::process::Command;

use steerable_sidecar::landlock::{
    landlock_abi, landlock_available, parse_launcher_argv, ruleset_attr, LandlockPlan,
    FS_MASK_ABI1, FS_REFER, FS_TRUNCATE, NET_BIND_TCP, NET_CONNECT_TCP,
};

#[test]
fn launcher_argv_roots_network_and_command() {
    let (roots, network, cmd) = parse_launcher_argv(
        &[
            "--root",
            "/a",
            "--root",
            "/b c",
            "--network",
            "--",
            "/bin/sh",
            "-c",
            "echo hi",
        ]
        .iter()
        .map(|s| (*s).to_string())
        .collect::<Vec<_>>(),
    )
    .unwrap();
    assert_eq!(roots, vec!["/a", "/b c"]);
    assert!(network);
    assert_eq!(cmd, vec!["/bin/sh", "-c", "echo hi"]);
}

#[test]
fn launcher_argv_defaults() {
    let (roots, network, cmd) = parse_launcher_argv(&["--".into(), "true".into()]).unwrap();
    assert!(roots.is_empty());
    assert!(!network);
    assert_eq!(cmd, vec!["true"]);
}

#[test]
fn launcher_argv_missing_separator_fails() {
    assert!(parse_launcher_argv(&["--root".into(), "/a".into()]).is_err());
    assert!(parse_launcher_argv(&["--".into()]).is_err());
}

#[test]
fn abi1_masks_to_v1_rights_only() {
    let attr = ruleset_attr(1, false);
    let handled_fs = u64::from_le_bytes(attr[..8].try_into().unwrap());
    assert_eq!(handled_fs, FS_MASK_ABI1);
    assert_eq!(attr.len(), 8);
}

#[test]
fn abi3_adds_refer_and_truncate() {
    let handled_fs = u64::from_le_bytes(ruleset_attr(3, false)[..8].try_into().unwrap());
    assert!(handled_fs & FS_REFER != 0);
    assert!(handled_fs & FS_TRUNCATE != 0);
}

#[test]
fn net_denied_only_when_abi4_and_undeclared() {
    let attr = ruleset_attr(4, false);
    let handled_net = u64::from_le_bytes(attr[8..16].try_into().unwrap());
    assert_eq!(handled_net, NET_BIND_TCP | NET_CONNECT_TCP);
}

#[test]
fn net_open_when_declared() {
    let attr = ruleset_attr(4, true);
    assert_eq!(u64::from_le_bytes(attr[8..16].try_into().unwrap()), 0);
}

#[test]
fn net_unenforceable_below_abi4() {
    assert_eq!(ruleset_attr(3, false).len(), 8);
}

#[test]
fn argv_routes_through_the_launcher() {
    let root = std::env::temp_dir();
    let plan = LandlockPlan::new(
        &[root.to_string_lossy().into_owned()],
        false,
        "/bin/sidecar",
        Some(4),
    )
    .unwrap();
    let argv = plan
        .argv_for_exec(&["/bin/sh".into(), "-c".into(), "true".into()])
        .unwrap();
    assert_eq!(
        &argv[..3],
        &[
            "/bin/sidecar".to_string(),
            "sandbox".into(),
            "landlock".into()
        ]
    );
    let root_i = argv.iter().position(|s| s == "--root").unwrap();
    let expected = root.canonicalize().unwrap_or(root.clone());
    assert_eq!(argv[root_i + 1], expected.to_string_lossy());
    assert!(!argv.contains(&"--network".to_string()));
    assert_eq!(&argv[argv.len() - 4..], ["--", "/bin/sh", "-c", "true"]);
}

#[test]
fn network_declared_adds_flag() {
    let argv = LandlockPlan::new(&[], true, "/bin/sidecar", Some(4))
        .unwrap()
        .argv_for_exec(&["true".into()])
        .unwrap();
    assert!(argv.contains(&"--network".to_string()));
}

#[test]
fn enforcement_full_only_when_net_denied_and_abi4() {
    assert_eq!(
        LandlockPlan::new(&[], false, "/bin/sidecar", Some(4))
            .unwrap()
            .enforcement(),
        "full"
    );
    assert_eq!(
        LandlockPlan::new(&[], false, "/bin/sidecar", Some(3))
            .unwrap()
            .enforcement(),
        "partial"
    );
    assert_eq!(
        LandlockPlan::new(&[], true, "/bin/sidecar", Some(4))
            .unwrap()
            .enforcement(),
        "partial"
    );
}

#[test]
fn writable_root_must_exist() {
    let err = LandlockPlan::new(
        &["/no/such/landlock-root".into()],
        false,
        "/bin/sidecar",
        Some(4),
    )
    .unwrap_err();
    assert!(err.contains("does not exist"));
}

#[test]
fn abi_zero_off_linux() {
    if cfg!(target_os = "linux") {
        return;
    }
    assert_eq!(landlock_abi(), 0);
    assert!(!landlock_available());
}

#[test]
fn cli_landlock_fails_loud_off_linux() {
    if cfg!(target_os = "linux") {
        return;
    }
    let output = Command::new(env!("CARGO_BIN_EXE_steerable-sidecar"))
        .args(["sandbox", "landlock", "--", "/bin/true"])
        .output()
        .expect("run landlock launcher");
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("steerable-landlock"));
}
