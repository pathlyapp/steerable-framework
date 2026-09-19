//! OS sandbox profile generation (Python `steerable_sidecar.sandbox`).

use std::path::{Path, PathBuf};

pub use crate::landlock::{landlock_available, LandlockPlan};

pub const MACOS_SEATBELT_EXECUTABLE: &str = "/usr/bin/sandbox-exec";
pub const BWRAP_CANDIDATES: &[&str] = &["/usr/bin/bwrap", "/bin/bwrap"];

const BASE_POLICY: &str = r#"(version 1)
(deny default)

; Child processes inherit this policy, so allowing exec is not an escape.
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))

; Python runtime queries (CPU info, hostname, OS version, page size).
; Broad read: these leak no user data and scoping risks denials that only
; surface under specific provider/model combinations.
(allow sysctl-read)

; User/group lookup (os.getpwuid via libinfo).
(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo")
)

; /dev/null sinks (Python opens it for subprocess redirection etc.).
(allow file-write-data
  (require-all
    (path "/dev/null")
    (vnode-type CHARACTER-DEVICE)))

; Reads stay open: skill roots are host-configured per request and the
; loop legitimately reads user-chosen directories. Confinement targets
; writes and execution, not reads.
(allow file-read*)

; System scratch dirs.
(allow file-read* file-test-existence file-write* (subpath "/tmp"))
(allow file-read* file-write* (subpath "/private/tmp"))
(allow file-read* file-write* (subpath "/var/tmp"))
(allow file-read* file-write* (subpath "/private/var/tmp"))
"#;

const NETWORK_SERVICES: &str = r#"(allow system-socket
  (require-all
    (socket-domain AF_SYSTEM)
    (socket-protocol 2)
  )
)

(allow mach-lookup
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.system.opendirectoryd.membership")
  (global-name "com.apple.SecurityServer")
  (global-name "com.apple.networkd")
  (global-name "com.apple.ocspd")
  (global-name "com.apple.trustd.agent")
  (global-name "com.apple.SystemConfiguration.DNSConfiguration")
  (global-name "com.apple.SystemConfiguration.configd")
)

(allow sysctl-read
  (sysctl-name-regex #"^net.routetable")
)
"#;

const NETWORK_POLICY: &str = "(allow network-outbound)\n";

const LOCALHOST_NAMES: &[&str] = &["localhost", "127.0.0.1", "::1", "[::1]"];
const DEFAULT_EGRESS_PORTS: [u16; 2] = [443, 80];

const RESOLVER_EGRESS_POLICY: &str = r#"
; name resolution only (no IP reach): the system resolver's UNIX socket.
(allow network-outbound (literal "/private/var/run/mDNSResponder"))
"#;

const WEB_EGRESS_POLICY: &str = r#"
; network-read tools (web_fetch/web_search): the model chooses the target, so
; only the resolver socket and the http(s) ports can be declared ahead of it.
(allow network-outbound (literal "/private/var/run/mDNSResponder"))
(allow network-outbound (remote tcp "*:443"))
(allow network-outbound (remote tcp "*:80"))
"#;

pub fn seatbelt_available() -> bool {
    cfg!(target_os = "macos") && Path::new(MACOS_SEATBELT_EXECUTABLE).is_file()
}

pub fn parse_host_entry(entry: &str) -> Result<(String, Vec<u16>), String> {
    let text = entry.trim();
    let (host, ports) = match text.rsplit_once(':') {
        Some((host, port_s))
            if !host.is_empty()
                && !port_s.is_empty()
                && port_s.chars().all(|c| c.is_ascii_digit()) =>
        {
            let port: u16 = port_s
                .parse()
                .map_err(|_| format!("invalid allow-list entry {entry:?}: port out of range"))?;
            if port == 0 {
                return Err(format!(
                    "invalid allow-list entry {entry:?}: port out of range"
                ));
            }
            (host, vec![port])
        }
        Some(_) => {
            return Err(format!(
                "invalid allow-list entry {entry:?}: expected host or host:port (letters, digits, dot, dash, underscore)"
            ));
        }
        None => (text, DEFAULT_EGRESS_PORTS.to_vec()),
    };
    if host.is_empty()
        || !host
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
    {
        return Err(format!(
            "invalid allow-list entry {entry:?}: expected host or host:port (letters, digits, dot, dash, underscore)"
        ));
    }
    Ok((host.to_string(), ports))
}

fn sbpl_string(path: &str) -> String {
    format!("\"{}\"", path.replace('\\', "\\\\").replace('"', "\\\""))
}

fn expand_user(path: &str) -> String {
    if path == "~" || path.starts_with("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            let mut out = PathBuf::from(home);
            if path.len() > 2 {
                out.push(&path[2..]);
            }
            return out.to_string_lossy().into_owned();
        }
    }
    path.to_string()
}

pub fn normalize_root(root: &str) -> PathBuf {
    let expanded = expand_user(root);
    let path = PathBuf::from(&expanded);
    let abs = if path.is_absolute() {
        path
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    };
    match abs.canonicalize() {
        Ok(canon) => canon,
        Err(_) => match abs.parent().and_then(|parent| parent.canonicalize().ok()) {
            Some(parent) => parent.join(abs.file_name().unwrap_or_default()),
            None => abs,
        },
    }
}

fn egress_policy(allowed_hosts: &[String]) -> Result<String, String> {
    let mut rules = Vec::new();
    let mut seen = std::collections::HashSet::new();
    let mut degraded = false;
    for entry in allowed_hosts {
        let (host, ports) = parse_host_entry(entry)?;
        for port in ports {
            let rule = if LOCALHOST_NAMES.contains(&host.as_str()) {
                format!(r#"(allow network-outbound (remote tcp "localhost:{port}"))"#)
            } else {
                degraded = true;
                format!(r#"(allow network-outbound (remote tcp "*:{port}"))"#)
            };
            if seen.insert(rule.clone()) {
                rules.push(rule);
            }
        }
    }
    let mut header =
        "; egress allow-list (fail-closed): outbound denied except the endpoints below."
            .to_string();
    if degraded {
        header.push_str(
            "\n; NOTE: sbpl cannot match hostnames — non-localhost entries are\
             \n; enforced by port only. For per-host enforcement, proxy egress\
             \n; through a local allow-listing proxy and declare localhost:<port>.",
        );
    }
    Ok(format!(
        "{header}\n{}\n\n{NETWORK_SERVICES}",
        rules.join("\n")
    ))
}

pub fn build_seatbelt_profile(
    writable_roots: &[String],
    network: bool,
    allowed_hosts: Option<&[String]>,
    web_egress: bool,
    resolver: bool,
) -> Result<String, String> {
    let mut parts = vec![BASE_POLICY.to_string()];
    for root in writable_roots {
        let normalized = normalize_root(root);
        parts.push(format!(
            "; host-declared writable root\n(allow file-read* file-write* (subpath {}))\n",
            sbpl_string(&normalized.to_string_lossy())
        ));
    }
    if network {
        match allowed_hosts {
            None => {
                parts.push(format!("{NETWORK_POLICY}{NETWORK_SERVICES}"));
            }
            Some(hosts) => {
                parts.push(egress_policy(hosts)?);
                if web_egress {
                    parts.push(WEB_EGRESS_POLICY.to_string());
                } else if resolver {
                    parts.push(RESOLVER_EGRESS_POLICY.to_string());
                }
            }
        }
    }
    Ok(parts.join("\n"))
}

pub fn seatbelt_argv(profile: &str, argv: &[String]) -> Vec<String> {
    let mut out = vec![
        MACOS_SEATBELT_EXECUTABLE.to_string(),
        "-p".into(),
        profile.to_string(),
    ];
    out.extend(argv.iter().cloned());
    out
}

pub fn seatbelt_enforcement(network: bool, allowed_hosts: Option<&[String]>) -> &'static str {
    if !network {
        return "full";
    }
    if let Some(hosts) = allowed_hosts {
        if hosts.iter().all(|entry| {
            parse_host_entry(entry).is_ok_and(|(host, _)| LOCALHOST_NAMES.contains(&host.as_str()))
        }) {
            return "full";
        }
    }
    "partial"
}

pub struct BwrapPlan {
    pub executable: String,
    pub roots: Vec<PathBuf>,
    pub network: bool,
}

impl BwrapPlan {
    pub fn new(writable_roots: &[String], network: bool, executable: &str) -> Result<Self, String> {
        let mut roots = Vec::new();
        for root in writable_roots {
            let normalized = normalize_root(root);
            if !normalized.is_dir() {
                return Err(format!(
                    "writable root {root:?} does not exist: bwrap bind sources must exist before the command runs"
                ));
            }
            roots.push(normalized);
        }
        Ok(Self {
            executable: executable.to_string(),
            roots,
            network,
        })
    }

    pub fn enforcement(&self) -> &'static str {
        if self.network {
            "partial"
        } else {
            "full"
        }
    }

    pub fn profile_args(&self) -> Vec<String> {
        let mut args = vec![
            "--ro-bind".into(),
            "/".into(),
            "/".into(),
            "--dev".into(),
            "/dev".into(),
            "--unshare-pid".into(),
            "--proc".into(),
            "/proc".into(),
            "--die-with-parent".into(),
            "--tmpfs".into(),
            "/tmp".into(),
        ];
        if !self.network {
            args.push("--unshare-net".into());
        }
        for root in &self.roots {
            args.push("--bind".into());
            args.push(root.to_string_lossy().into_owned());
            args.push(root.to_string_lossy().into_owned());
        }
        args
    }

    pub fn argv_for_exec(&self, argv: &[String]) -> Result<Vec<String>, String> {
        if argv.is_empty() {
            return Err("linux-wrap command argv is empty".into());
        }
        let mut out = vec![self.executable.clone()];
        out.extend(self.profile_args());
        out.push("--".into());
        out.extend(argv.iter().cloned());
        Ok(out)
    }

    pub fn argv_for_shell(&self, command: &str) -> Result<Vec<String>, String> {
        self.argv_for_exec(&["/bin/sh".into(), "-c".into(), command.into()])
    }
}

pub fn linux_process_wrap(
    argv: &[String],
    writable_roots: &[String],
    network: bool,
) -> Result<serde_json::Value, String> {
    if argv.is_empty() {
        return Err("linux-wrap command argv is empty".into());
    }
    if let Some(executable) = BWRAP_CANDIDATES
        .iter()
        .copied()
        .find(|path| Path::new(path).is_file())
    {
        let plan = BwrapPlan::new(writable_roots, network, executable)?;
        let wrapped = plan.argv_for_exec(argv)?;
        return Ok(serde_json::json!({
            "argv": wrapped,
            "backend": "bwrap",
            "enforcement": plan.enforcement(),
        }));
    }
    if landlock_available() {
        let executable = std::env::current_exe()
            .ok()
            .map(|path| path.to_string_lossy().into_owned())
            .unwrap_or_else(|| "steerable-sidecar".into());
        let plan = LandlockPlan::new(writable_roots, network, &executable, None)?;
        let wrapped = plan.argv_for_exec(argv)?;
        return Ok(serde_json::json!({
            "argv": wrapped,
            "backend": "landlock",
            "enforcement": plan.enforcement(),
        }));
    }
    Err("no Linux process sandbox (bwrap and Landlock both unavailable)".into())
}

pub fn confine_exec(
    argv: &[String],
    writable_roots: &[String],
    network: bool,
) -> Result<(Vec<String>, serde_json::Value), String> {
    if argv.is_empty() {
        return Err("command argv is empty".into());
    }
    if seatbelt_available() {
        let profile = build_seatbelt_profile(writable_roots, network, None, false, false)?;
        return Ok((
            seatbelt_argv(&profile, argv),
            serde_json::json!({
                "backend": "seatbelt",
                "enforcement": seatbelt_enforcement(network, None),
            }),
        ));
    }
    let wrapped = linux_process_wrap(argv, writable_roots, network)?;
    let argv = wrapped["argv"]
        .as_array()
        .ok_or_else(|| "sandbox wrap missing argv".to_string())?
        .iter()
        .filter_map(|value| value.as_str().map(str::to_string))
        .collect();
    Ok((
        argv,
        serde_json::json!({
            "backend": wrapped["backend"],
            "enforcement": wrapped["enforcement"],
        }),
    ))
}

pub fn sandbox_cli(args: &[String]) -> i32 {
    if args.first().map(String::as_str) != Some("sandbox") {
        return 2;
    }
    let rest = &args[1..];
    match rest.first().map(String::as_str) {
        Some("profile") => match parse_profile_args(&rest[1..]) {
            Ok((roots, network, hosts, web, resolver)) => {
                match build_seatbelt_profile(&roots, network, hosts.as_deref(), web, resolver) {
                    Ok(profile) => {
                        print!("{profile}");
                        0
                    }
                    Err(err) => {
                        eprintln!("error: {err}");
                        2
                    }
                }
            }
            Err(err) => {
                eprintln!("error: {err}");
                2
            }
        },
        Some("linux-wrap") => match parse_linux_wrap_args(&rest[1..]) {
            Ok((roots, network, argv)) => match linux_process_wrap(&argv, &roots, network) {
                Ok(plan) => {
                    println!("{}", serde_json::to_string(&plan).unwrap());
                    0
                }
                Err(err) => {
                    eprintln!("{err}");
                    2
                }
            },
            Err(err) => {
                eprintln!("error: {err}");
                2
            }
        },
        Some("landlock") => match crate::landlock::run_launcher(&rest[1..]) {
            Ok(code) => code,
            Err(err) => {
                eprintln!("steerable-landlock: {err}");
                1
            }
        },
        _ => {
            eprintln!("error: expected sandbox profile|linux-wrap|landlock");
            2
        }
    }
}

fn parse_profile_args(
    args: &[String],
) -> Result<(Vec<String>, bool, Option<Vec<String>>, bool, bool), String> {
    let mut roots = Vec::new();
    let mut network = true;
    let mut hosts: Option<Vec<String>> = None;
    let mut web = false;
    let mut resolver = false;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--writable-root" => {
                i += 1;
                roots.push(args.get(i).cloned().ok_or("--writable-root needs a path")?);
            }
            "--no-network" => network = false,
            "--allow-host" => {
                i += 1;
                hosts
                    .get_or_insert_with(Vec::new)
                    .push(args.get(i).cloned().ok_or("--allow-host needs a value")?);
            }
            "--allow-web-egress" => web = true,
            "--allow-resolver" => resolver = true,
            other => return Err(format!("unknown argument {other}")),
        }
        i += 1;
    }
    Ok((roots, network, hosts, web, resolver))
}

pub fn describe_exec_sandbox(network: bool, allowed_hosts: Option<&[String]>) -> serde_json::Value {
    if seatbelt_available() {
        return serde_json::json!({
            "backend": "seatbelt",
            "enforcement": seatbelt_enforcement(network, allowed_hosts),
        });
    }
    if BWRAP_CANDIDATES
        .iter()
        .any(|path| Path::new(path).is_file())
    {
        return serde_json::json!({
            "backend": "bwrap",
            "enforcement": if network { "partial" } else { "full" },
        });
    }
    if landlock_available() {
        let abi = crate::landlock::landlock_abi();
        return serde_json::json!({
            "backend": "landlock",
            "enforcement": if network || abi < 4 { "partial" } else { "full" },
        });
    }
    serde_json::json!({"backend": "none", "enforcement": "none"})
}

fn parse_linux_wrap_args(args: &[String]) -> Result<(Vec<String>, bool, Vec<String>), String> {
    let mut roots = Vec::new();
    let mut network = true;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--writable-root" => {
                i += 1;
                roots.push(args.get(i).cloned().ok_or("--writable-root needs a path")?);
            }
            "--no-network" => network = false,
            "--" => {
                let mut argv = args[i + 1..].to_vec();
                if argv.first().map(String::as_str) == Some("--") {
                    argv.remove(0);
                }
                return Ok((roots, network, argv));
            }
            _other => {
                return Ok((roots, network, args[i..].to_vec()));
            }
        }
        i += 1;
    }
    Err("linux-wrap command argv is empty".into())
}
