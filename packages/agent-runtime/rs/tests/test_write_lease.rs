use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use steerable_agent_runtime::{acquire_write_lease, lock_path_for_db, LeaseError};

struct TmpDir {
    path: PathBuf,
}

impl TmpDir {
    fn new(label: &str) -> Self {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("steerable-{label}-{}-{nanos}", std::process::id()));
        std::fs::create_dir_all(&path).expect("tmp dir");
        Self { path }
    }
}

impl Drop for TmpDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

#[test]
fn holder_process_entry() {
    let Ok(db) = std::env::var("STEERABLE_WRITE_LEASE_DB") else {
        return;
    };
    let flag = std::env::var("STEERABLE_WRITE_LEASE_FLAG").expect("flag");
    let _lease = acquire_write_lease(&db).expect("holder lease");
    std::fs::write(&flag, "held").expect("flag");
    std::thread::sleep(Duration::from_secs(60));
}

fn spawn_holder(db: &Path, flag: &Path) -> std::process::Child {
    Command::new(std::env::current_exe().expect("test bin"))
        .arg("holder_process_entry")
        .arg("--exact")
        .env("STEERABLE_WRITE_LEASE_DB", db)
        .env("STEERABLE_WRITE_LEASE_FLAG", flag)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn holder")
}

fn wait_held(child: &mut std::process::Child, flag: &Path) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if let Some(code) = child.try_wait().unwrap() {
            let mut err = String::new();
            if let Some(mut stderr) = child.stderr.take() {
                use std::io::Read;
                let _ = stderr.read_to_string(&mut err);
            }
            panic!("holder exited {code} before taking the lease: {err}");
        }
        if flag.is_file() && std::fs::read_to_string(flag).unwrap_or_default().trim() == "held" {
            return;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    panic!("holder did not take the lease");
}

#[test]
fn lock_path_is_sibling_lock_file() {
    let dir = TmpDir::new("lock-path");
    let db = dir.path.join("sessions.db");
    assert_eq!(lock_path_for_db(&db), dir.path.join("sessions.lock"));
}

#[test]
fn second_process_fails_loud() {
    let dir = TmpDir::new("second-writer");
    let db = dir.path.join("sessions.db");
    let flag = dir.path.join("held");
    let mut holder = spawn_holder(&db, &flag);
    wait_held(&mut holder, &flag);
    let err = acquire_write_lease(&db).expect_err("second writer");
    match err {
        LeaseError::AlreadyOwned(owned) => {
            assert!(owned.to_string().contains("already owned"), "{owned}");
        }
        other => panic!("expected already owned, got {other}"),
    }
    assert!(lock_path_for_db(&db).is_file());
    holder.kill().ok();
    let _ = holder.wait();
}

#[test]
fn successor_opens_after_holder_is_killed() {
    let dir = TmpDir::new("successor");
    let db = dir.path.join("sessions.db");
    let flag = dir.path.join("held");
    let mut holder = spawn_holder(&db, &flag);
    wait_held(&mut holder, &flag);
    holder.kill().ok();
    holder.wait().unwrap();
    let mut lease = acquire_write_lease(&db).expect("successor");
    lease.release();
    assert!(lock_path_for_db(&db).is_file());
}

#[test]
fn release_does_not_delete_lock_file() {
    let dir = TmpDir::new("keep-lock");
    let db = dir.path.join("sessions.db");
    let mut lease = acquire_write_lease(&db).unwrap();
    let lock = lock_path_for_db(&db);
    assert!(lock.is_file());
    lease.release();
    assert!(lock.is_file());
    let mut again = acquire_write_lease(&db).unwrap();
    again.release();
    assert!(lock.is_file());
}
