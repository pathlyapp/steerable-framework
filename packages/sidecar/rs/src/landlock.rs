//! Linux Landlock per-exec backend (Python `steerable_sidecar.landlock`).

#[cfg(target_os = "linux")]
use std::path::Path;
use std::path::PathBuf;
use std::sync::OnceLock;

use crate::sandbox::normalize_root;

pub const FS_EXECUTE: u64 = 1 << 0;
pub const FS_WRITE_FILE: u64 = 1 << 1;
pub const FS_READ_FILE: u64 = 1 << 2;
pub const FS_READ_DIR: u64 = 1 << 3;
pub const FS_REMOVE_DIR: u64 = 1 << 4;
pub const FS_REMOVE_FILE: u64 = 1 << 5;
pub const FS_MAKE_CHAR: u64 = 1 << 6;
pub const FS_MAKE_DIR: u64 = 1 << 7;
pub const FS_MAKE_REG: u64 = 1 << 8;
pub const FS_MAKE_SOCK: u64 = 1 << 9;
pub const FS_MAKE_FIFO: u64 = 1 << 10;
pub const FS_MAKE_BLOCK: u64 = 1 << 11;
pub const FS_MAKE_SYM: u64 = 1 << 12;
pub const FS_REFER: u64 = 1 << 13;
pub const FS_TRUNCATE: u64 = 1 << 14;
pub const FS_MASK_ABI1: u64 = (1 << 13) - 1;
pub const NET_BIND_TCP: u64 = 1 << 0;
pub const NET_CONNECT_TCP: u64 = 1 << 1;

#[cfg(target_os = "linux")]
const READ_ONLY_ROOT_RIGHTS: u64 = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR;
#[cfg(target_os = "linux")]
const SCRATCH_PATHS: &[&str] = &["/tmp", "/var/tmp"];

pub fn landlock_abi() -> u32 {
    #[cfg(target_os = "linux")]
    {
        linux_abi()
    }
    #[cfg(not(target_os = "linux"))]
    {
        0
    }
}

pub fn fs_mask(abi: u32) -> u64 {
    let mut mask = FS_MASK_ABI1;
    if abi >= 2 {
        mask |= FS_REFER;
    }
    if abi >= 3 {
        mask |= FS_TRUNCATE;
    }
    mask
}

pub fn ruleset_attr(abi: u32, network: bool) -> Vec<u8> {
    let handled_fs = fs_mask(abi);
    if abi >= 4 {
        let handled_net = if network {
            0
        } else {
            NET_BIND_TCP | NET_CONNECT_TCP
        };
        let mut buf = Vec::with_capacity(16);
        buf.extend_from_slice(&handled_fs.to_le_bytes());
        buf.extend_from_slice(&handled_net.to_le_bytes());
        buf
    } else {
        handled_fs.to_le_bytes().to_vec()
    }
}

pub fn parse_launcher_argv(argv: &[String]) -> Result<(Vec<String>, bool, Vec<String>), String> {
    let mut roots = Vec::new();
    let mut network = false;
    let mut i = 0;
    while i < argv.len() {
        match argv[i].as_str() {
            "--" => {
                let cmd = argv[i + 1..].to_vec();
                if cmd.is_empty() {
                    return Err("missing command after '--'".into());
                }
                return Ok((roots, network, cmd));
            }
            "--root" => {
                i += 1;
                roots.push(
                    argv.get(i)
                        .cloned()
                        .ok_or_else(|| "--root needs a path argument".to_string())?,
                );
            }
            "--network" => network = true,
            other => return Err(format!("unknown launcher flag {other:?}")),
        }
        i += 1;
    }
    Err("missing '--' separator before the command".into())
}

#[derive(Debug)]
pub struct LandlockPlan {
    pub executable: String,
    pub roots: Vec<PathBuf>,
    pub network: bool,
    pub abi: u32,
}

impl LandlockPlan {
    pub fn new(
        writable_roots: &[String],
        network: bool,
        executable: &str,
        abi: Option<u32>,
    ) -> Result<Self, String> {
        let mut roots = Vec::new();
        for root in writable_roots {
            let normalized = normalize_root(root);
            if !normalized.is_dir() {
                return Err(format!(
                    "writable root {root:?} does not exist: landlock rules open the path when the launcher installs them"
                ));
            }
            roots.push(normalized);
        }
        Ok(Self {
            executable: executable.to_string(),
            roots,
            network,
            abi: abi.unwrap_or_else(landlock_abi),
        })
    }

    pub fn enforcement(&self) -> &'static str {
        if self.network {
            "partial"
        } else if self.abi >= 4 {
            "full"
        } else {
            "partial"
        }
    }

    pub fn argv_for_exec(&self, argv: &[String]) -> Result<Vec<String>, String> {
        if argv.is_empty() {
            return Err("linux-wrap command argv is empty".into());
        }
        let mut wrapped = vec![self.executable.clone(), "sandbox".into(), "landlock".into()];
        for root in &self.roots {
            wrapped.push("--root".into());
            wrapped.push(root.to_string_lossy().into_owned());
        }
        if self.network {
            wrapped.push("--network".into());
        }
        wrapped.push("--".into());
        wrapped.extend(argv.iter().cloned());
        Ok(wrapped)
    }
}

pub fn landlock_available() -> bool {
    if !cfg!(target_os = "linux") {
        return false;
    }
    static CACHED: OnceLock<bool> = OnceLock::new();
    *CACHED.get_or_init(probe_landlock)
}

fn probe_landlock() -> bool {
    if landlock_abi() < 1 {
        return false;
    }
    let Ok(exe) = std::env::current_exe() else {
        return false;
    };
    std::process::Command::new(exe)
        .args(["sandbox", "landlock", "--", "/bin/true"])
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false)
}

pub fn run_launcher(args: &[String]) -> Result<i32, String> {
    let (roots, network, cmd) = parse_launcher_argv(args)?;
    install_ruleset(&roots, network)?;
    unix_exec(&cmd)
}

fn unix_exec(cmd: &[String]) -> Result<i32, String> {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let mut command = std::process::Command::new(&cmd[0]);
        command.args(&cmd[1..]);
        let err = command.exec();
        Err(format!("exec {}: {err}", cmd[0]))
    }
    #[cfg(not(unix))]
    {
        let _ = cmd;
        Err("Landlock launcher requires a Unix exec".into())
    }
}

fn install_ruleset(roots: &[String], network: bool) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    {
        linux_install(roots, network)
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = (roots, network);
        Err("Landlock is not supported on this kernel".into())
    }
}

#[cfg(target_os = "linux")]
fn linux_abi() -> u32 {
    let ret = unsafe { libc::syscall(444, std::ptr::null::<libc::c_void>(), 0usize, 1u32) };
    if ret < 0 {
        0
    } else {
        ret as u32
    }
}

#[cfg(target_os = "linux")]
fn linux_install(roots: &[String], network: bool) -> Result<(), String> {
    let abi = landlock_abi();
    if abi < 1 {
        return Err("Landlock is not supported on this kernel".into());
    }
    let attr = ruleset_attr(abi, network);
    let handled_fs = u64::from_le_bytes(attr[..8].try_into().unwrap());
    let ruleset_fd = unsafe { libc::syscall(444, attr.as_ptr(), attr.len(), 0u32) };
    if ruleset_fd < 0 {
        return Err(format!(
            "landlock_create_ruleset: {}",
            std::io::Error::last_os_error()
        ));
    }
    let ruleset_fd = ruleset_fd as i32;
    let result = (|| {
        add_path_rule(ruleset_fd, "/", READ_ONLY_ROOT_RIGHTS, handled_fs)?;
        for path in SCRATCH_PATHS {
            if Path::new(path).exists() {
                add_path_rule(ruleset_fd, path, handled_fs, handled_fs)?;
            }
        }
        for root in roots {
            add_path_rule(ruleset_fd, root, handled_fs, handled_fs)?;
        }
        add_path_rule(
            ruleset_fd,
            "/dev/null",
            FS_READ_FILE | FS_WRITE_FILE,
            handled_fs,
        )?;
        let prctl = unsafe { libc::prctl(38, 1, 0, 0, 0) };
        if prctl != 0 {
            return Err(format!(
                "prctl(NO_NEW_PRIVS): {}",
                std::io::Error::last_os_error()
            ));
        }
        let ret = unsafe { libc::syscall(446, ruleset_fd, 0u32) };
        if ret < 0 {
            return Err(format!(
                "landlock_restrict_self: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(())
    })();
    unsafe {
        libc::close(ruleset_fd);
    }
    result
}

#[cfg(target_os = "linux")]
fn add_path_rule(ruleset_fd: i32, path: &str, rights: u64, handled_fs: u64) -> Result<(), String> {
    let rights = rights & handled_fs;
    if rights == 0 {
        return Ok(());
    }
    let fd = unsafe { libc::open(std::ffi::CString::new(path).unwrap().as_ptr(), libc::O_PATH) };
    if fd < 0 {
        return Err(format!(
            "landlock_add_rule({path}): {}",
            std::io::Error::last_os_error()
        ));
    }
    #[repr(C, packed)]
    struct PathBeneath {
        allowed_access: u64,
        parent_fd: i32,
    }
    let attr = PathBeneath {
        allowed_access: rights,
        parent_fd: fd,
    };
    let ret = unsafe { libc::syscall(445, ruleset_fd, 1u32, &attr as *const PathBeneath, 0u32) };
    unsafe {
        libc::close(fd);
    }
    if ret < 0 {
        return Err(format!(
            "landlock_add_rule({path}): {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(())
}
