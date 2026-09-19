use std::process::Command;

use steerable_sidecar::sandbox::{
    build_seatbelt_profile, describe_exec_sandbox, seatbelt_argv, seatbelt_available, BwrapPlan,
    MACOS_SEATBELT_EXECUTABLE,
};

fn profile(
    roots: &[&str],
    network: bool,
    hosts: Option<&[&str]>,
    web: bool,
    resolver: bool,
) -> String {
    let roots: Vec<String> = roots.iter().map(|s| (*s).to_string()).collect();
    let hosts = hosts.map(|entries| entries.iter().map(|s| (*s).to_string()).collect::<Vec<_>>());
    build_seatbelt_profile(&roots, network, hosts.as_deref(), web, resolver).expect("profile")
}

#[test]
fn profile_is_closed_by_default() {
    let text = profile(&[], true, None, false, false);
    assert!(text.contains("(deny default)"));
    assert!(!text.contains("network-bind"));
}

#[test]
fn profile_allows_broad_reads_and_outbound_network() {
    let text = profile(&[], true, None, false, false);
    assert!(text.contains("(allow file-read*)"));
    assert!(text.contains("(allow network-outbound)"));
    assert!(text.contains("com.apple.SystemConfiguration.DNSConfiguration"));
}

#[test]
fn profile_no_network_omits_outbound() {
    let text = profile(&[], false, None, false, false);
    assert!(!text.contains("network-outbound"));
}

#[test]
fn profile_writable_roots_are_normalized_literals() {
    let text = profile(&["~/Library/Caches/x"], true, None, false, false);
    assert!(text.contains("(subpath \""));
    assert!(!text.contains('~'));
}

#[test]
fn profile_writable_root_escapes_quotes() {
    let text = profile(&[r#"/tmp/we"ird"#], true, None, false, false);
    assert!(text.contains(r#"\""#));
}

#[test]
fn profile_without_writable_roots_has_four_scratch_writes() {
    let text = profile(&[], true, None, false, false);
    assert_eq!(text.matches("file-write*").count(), 4);
}

#[test]
fn seatbelt_argv_wraps_with_inline_profile() {
    let argv = seatbelt_argv(
        "(deny default)",
        &[
            "/usr/bin/python3".into(),
            "-m".into(),
            "steerable_sidecar".into(),
        ],
    );
    assert_eq!(
        argv[..3],
        [
            MACOS_SEATBELT_EXECUTABLE.to_string(),
            "-p".into(),
            "(deny default)".into()
        ]
    );
}

#[test]
fn allow_list_localhost_entry_pins_host_and_port() {
    let text = profile(&[], true, Some(&["127.0.0.1:11434"]), false, false);
    assert!(text.contains(r#"(remote tcp "localhost:11434")"#));
    assert!(!text.contains("\n(allow network-outbound)\n"));
    assert!(text.contains("com.apple.SystemConfiguration.DNSConfiguration"));
}

#[test]
fn allow_list_bare_remote_host_degrades_to_ports() {
    let text = profile(&[], true, Some(&["api.openai.com"]), false, false);
    assert!(text.contains(r#"(remote tcp "*:443")"#));
    assert!(text.contains(r#"(remote tcp "*:80")"#));
    assert!(text.contains("cannot match hostnames"));
}

#[test]
fn allow_list_explicit_port_and_dedup() {
    let text = profile(
        &[],
        true,
        Some(&["api.deepseek.com:8443", "api.deepseek.com:8443"]),
        false,
        false,
    );
    assert_eq!(text.matches(r#"(remote tcp "*:8443")"#).count(), 1);
    assert!(!text.contains("*:443"));
}

#[test]
fn allow_list_empty_list_denies_all_outbound() {
    let empty: &[String] = &[];
    let text = build_seatbelt_profile(&[], true, Some(empty), false, false).unwrap();
    assert!(!text.contains("network-outbound"));
    assert!(text.contains("com.apple.SystemConfiguration.configd"));
}

#[test]
fn web_egress_adds_resolver_and_http_ports() {
    let text = profile(&[], true, Some(&["127.0.0.1:11434"]), true, false);
    assert!(text.contains(r#"(literal "/private/var/run/mDNSResponder")"#));
    assert!(text.contains(r#"(remote tcp "*:443")"#));
    assert!(text.contains(r#"(remote tcp "*:80")"#));
}

#[test]
fn resolver_adds_only_the_resolver_socket() {
    let text = profile(&[], true, Some(&["127.0.0.1:8899"]), false, true);
    assert!(text.contains(r#"(literal "/private/var/run/mDNSResponder")"#));
    assert!(!text.contains(r#"(remote tcp "*:443")"#));
}

#[test]
fn bwrap_profile_pins_namespace_invariants() {
    let plan = BwrapPlan::new(&[], false, "/usr/bin/bwrap").unwrap();
    let args = plan.argv_for_shell("true").unwrap();
    assert!(args.contains(&"--unshare-pid".to_string()));
    assert!(args.contains(&"--proc".to_string()));
    assert!(args.contains(&"--ro-bind".to_string()));
    assert!(args.contains(&"--die-with-parent".to_string()));
    assert_eq!(&args[args.len() - 4..], ["--", "/bin/sh", "-c", "true"]);
    assert!(args.contains(&"--unshare-net".to_string()));
    assert_eq!(plan.enforcement(), "full");
}

#[test]
fn bwrap_network_declared_shares_host_network() {
    let plan = BwrapPlan::new(&[], true, "/usr/bin/bwrap").unwrap();
    let args = plan.argv_for_shell("true").unwrap();
    assert!(!args.contains(&"--unshare-net".to_string()));
    assert_eq!(plan.enforcement(), "partial");
}

#[test]
fn sandbox_describe_reports_seatbelt_on_macos() {
    if !seatbelt_available() {
        return;
    }
    let pinned = describe_exec_sandbox(true, Some(&["127.0.0.1:8899".into()]));
    assert_eq!(pinned["backend"], "seatbelt");
    assert_eq!(pinned["enforcement"], "full");
    let open = describe_exec_sandbox(true, Some(&["api.deepseek.com:443".into()]));
    assert_eq!(open["enforcement"], "partial");
}

#[test]
fn cli_prints_profile() {
    let output = Command::new(env!("CARGO_BIN_EXE_steerable-sidecar"))
        .args([
            "sandbox",
            "profile",
            "--writable-root",
            "/tmp/x",
            "--no-network",
        ])
        .output()
        .expect("run sandbox profile");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("(deny default)"));
    assert!(stdout.contains("/private/tmp") || stdout.contains("/tmp/x"));
    assert!(!stdout.contains("network-outbound"));
}

#[test]
fn profile_actually_confines_a_child_process() {
    if !seatbelt_available() {
        return;
    }
    let text = profile(&[], false, None, false, false);
    let ok = Command::new(MACOS_SEATBELT_EXECUTABLE)
        .args(["-p", &text, "/bin/sh", "-c", "test -f /etc/hosts"])
        .output()
        .expect("sandbox-exec read");
    assert!(
        ok.status.success(),
        "{}",
        String::from_utf8_lossy(&ok.stderr)
    );

    let denied = Command::new(MACOS_SEATBELT_EXECUTABLE)
        .args(["-p", &text, "/bin/sh", "-c", "echo x > /etc/sb-denied-test"])
        .output()
        .expect("sandbox-exec write");
    assert!(!denied.status.success());
}
