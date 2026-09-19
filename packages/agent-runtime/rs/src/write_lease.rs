//! Cross-process write lease for one sqlite database file.
//!
//! POSIX takes a non-blocking `flock` on a sibling `*.lock` file
//! (`sessions.db` → `sessions.lock`). Windows opens that file with no share
//! mode. The lock file is never deleted: it keeps a stable inode for later
//! lockers. Readers do not take this lease.

use std::path::{Path, PathBuf};

const LOCK_ATTEMPTS: usize = 3;

#[derive(Debug)]
pub struct StoreAlreadyOwnedError {
    pub path: String,
}

impl std::fmt::Display for StoreAlreadyOwnedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "store already owned: {} (another process has this sqlite database open for write)",
            self.path
        )
    }
}

impl std::error::Error for StoreAlreadyOwnedError {}

#[derive(Debug)]
pub enum LeaseError {
    AlreadyOwned(StoreAlreadyOwnedError),
    Io(std::io::Error),
}

impl std::fmt::Display for LeaseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::AlreadyOwned(error) => write!(f, "{error}"),
            Self::Io(error) => write!(f, "{error}"),
        }
    }
}

impl std::error::Error for LeaseError {}

pub fn lock_path_for_db(db_path: impl AsRef<Path>) -> PathBuf {
    let raw = db_path.as_ref();
    let absolute = if raw.is_absolute() {
        raw.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(raw)
    };
    absolute.with_extension("lock")
}

pub fn acquire_write_lease(db_path: impl AsRef<Path>) -> Result<WriteLease, LeaseError> {
    let raw = db_path.as_ref();
    let as_str = raw.to_string_lossy();
    if as_str == ":memory:" || as_str.starts_with("file::memory:") {
        return Ok(WriteLease::unheld());
    }
    let lock_path = lock_path_for_db(raw);
    if let Some(parent) = lock_path.parent() {
        std::fs::create_dir_all(parent).map_err(LeaseError::Io)?;
    }
    #[cfg(unix)]
    {
        return WriteLease::acquire_posix(&lock_path);
    }
    #[cfg(windows)]
    {
        return WriteLease::acquire_win32(&lock_path);
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = std::fs::OpenOptions::new()
            .create(true)
            .write(true)
            .open(&lock_path)
            .map_err(LeaseError::Io)?;
        Ok(WriteLease::unheld())
    }
}

#[derive(Debug)]
pub struct WriteLease {
    #[cfg(unix)]
    fd: Option<i32>,
    #[cfg(windows)]
    file: Option<std::fs::File>,
    released: bool,
}

impl WriteLease {
    fn unheld() -> Self {
        Self {
            #[cfg(unix)]
            fd: None,
            #[cfg(windows)]
            file: None,
            released: false,
        }
    }

    #[cfg(unix)]
    fn acquire_posix(lock_path: &Path) -> Result<Self, LeaseError> {
        use std::os::unix::ffi::OsStrExt;

        let c_path = std::ffi::CString::new(lock_path.as_os_str().as_bytes()).map_err(|_| {
            LeaseError::Io(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "lock path contains NUL",
            ))
        })?;
        for _ in 0..LOCK_ATTEMPTS {
            let fd = unsafe { libc::open(c_path.as_ptr(), libc::O_RDWR | libc::O_CREAT, 0o644) };
            if fd < 0 {
                return Err(LeaseError::Io(std::io::Error::last_os_error()));
            }
            let locked = unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) };
            if locked != 0 {
                let err = std::io::Error::last_os_error();
                unsafe { libc::close(fd) };
                let errno = err.raw_os_error();
                if errno == Some(libc::EAGAIN)
                    || errno == Some(libc::EWOULDBLOCK)
                    || errno == Some(libc::EACCES)
                {
                    return Err(LeaseError::AlreadyOwned(StoreAlreadyOwnedError {
                        path: lock_path.display().to_string(),
                    }));
                }
                return Err(LeaseError::Io(err));
            }
            let mut held: libc::stat = unsafe { std::mem::zeroed() };
            if unsafe { libc::fstat(fd, &mut held) } != 0 {
                let err = std::io::Error::last_os_error();
                unsafe { libc::close(fd) };
                return Err(LeaseError::Io(err));
            }
            let mut current: libc::stat = unsafe { std::mem::zeroed() };
            if unsafe { libc::stat(c_path.as_ptr(), &mut current) } != 0 {
                unsafe { libc::close(fd) };
                continue;
            }
            if held.st_ino == current.st_ino && held.st_dev == current.st_dev {
                return Ok(Self {
                    fd: Some(fd),
                    released: false,
                });
            }
            unsafe { libc::close(fd) };
        }
        Err(LeaseError::AlreadyOwned(StoreAlreadyOwnedError {
            path: lock_path.display().to_string(),
        }))
    }

    #[cfg(windows)]
    fn acquire_win32(lock_path: &Path) -> Result<Self, LeaseError> {
        use std::os::windows::fs::OpenOptionsExt;

        match std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .share_mode(0)
            .open(lock_path)
        {
            Ok(file) => Ok(Self {
                file: Some(file),
                released: false,
            }),
            Err(err)
                if err.kind() == std::io::ErrorKind::PermissionDenied
                    || err.raw_os_error() == Some(32) =>
            {
                Err(LeaseError::AlreadyOwned(StoreAlreadyOwnedError {
                    path: lock_path.display().to_string(),
                }))
            }
            Err(err) => Err(LeaseError::Io(err)),
        }
    }

    pub fn release(&mut self) {
        if self.released {
            return;
        }
        self.released = true;
        #[cfg(unix)]
        if let Some(fd) = self.fd.take() {
            unsafe { libc::close(fd) };
        }
        #[cfg(windows)]
        {
            self.file = None;
        }
    }
}

impl Drop for WriteLease {
    fn drop(&mut self) {
        self.release();
    }
}
