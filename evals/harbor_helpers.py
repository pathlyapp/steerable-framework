"""Pure Harbor adapter helpers (no Harbor import; safe for unit tests)."""

from __future__ import annotations

import shlex
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REMOTE_SRC = "/installed-agent/steerable"
_VENV_CACHE_DIR = _REPO_ROOT / "evals" / ".cache"
_UV_CACHE_NAME = "uv-0.9.5-x86_64-unknown-linux-gnu.tar.gz"
_UV_MIN_BYTES = 20_000_000
_APT_PYTHON_INSTALL = r"""
export DEBIAN_FRONTEND=noninteractive
# Clash to archive.ubuntu.com is slow; GHA has no proxy and should keep Canonical.
if [ -n "${HTTP_PROXY}${HTTPS_PROXY}${http_proxy}${https_proxy}" ]; then
  for f in /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources; do
    [ -f "$f" ] || continue
    sed -i \
      -e 's|http://archive.ubuntu.com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
      -e 's|https://archive.ubuntu.com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
      -e 's|http://security.ubuntu.com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
      -e 's|https://security.ubuntu.com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
      "$f"
  done
fi
apt_held() {
  for pid in /proc/[0-9]*; do
    argv0=$(tr '\0' '\n' < "$pid/cmdline" 2>/dev/null | sed -n '1p')
    argv1=$(tr '\0' '\n' < "$pid/cmdline" 2>/dev/null | sed -n '2p')
    case "$argv0 $argv1" in
      */usr/bin/apt-get*|*/usr/bin/dpkg*|*/usr/bin/apt\ *) return 0 ;;
    esac
    [ -d "$pid/fd" ] || continue
    for fd in "$pid"/fd/*; do
      link=$(readlink "$fd" 2>/dev/null) || continue
      case "$link" in
        /var/lib/dpkg/lock-frontend|/var/lib/dpkg/lock|/var/lib/apt/lists/lock)
          return 0
          ;;
      esac
    done
  done
  return 1
}
kill_apt() {
  for pid in /proc/[0-9]*; do
    argv0=$(tr '\0' '\n' < "$pid/cmdline" 2>/dev/null | sed -n '1p')
    argv1=$(tr '\0' '\n' < "$pid/cmdline" 2>/dev/null | sed -n '2p')
    case "$argv0 $argv1" in
      */usr/bin/apt-get*|*/usr/bin/dpkg*) kill -9 "${pid##*/}" 2>/dev/null || true ;;
    esac
  done
  sleep 2
  rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock /var/cache/apt/archives/lock
  dpkg --configure -a || true
}
wait_dpkg() {
  i=0
  while [ "$i" -lt 90 ]; do
    apt_held || return 0
    sleep 2
    i=$((i+1))
  done
  kill_apt
}
wait_dpkg
apt-get update && apt-get install -y python3 python3-pip python3-venv
""".strip()
# Packages require Python >=3.10. qemu-alpine-ssh (and Debian 11 images)
# ship 3.9.2; pip then fails with NonZeroAgentExitCodeError before the
# agent runs. Prefer an injected /usr/local/bin/uv (GHA host copy) so
# 3.9 need not pip-install uv. Distro python3.11/3.12 next; curl uv last.
_ENSURE_PYTHON_310 = r"""
ok() {
  p=/usr/local/bin/python3
  [ -x "$p" ] || p=python3
  "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
}
uv_py() {
  command -v uv >/dev/null 2>&1 || return 1
  export UV_PYTHON_INSTALL_DIR=/opt/uv-python
  i=0
  while [ "$i" -lt 3 ]; do
    uv python install 3.12 && break
    i=$((i+1))
    sleep 5
  done
  py=$(uv python find 3.12 2>/dev/null || true)
  if [ -z "$py" ] || [ ! -x "$py" ]; then
    uv python install 3.11 || true
    py=$(uv python find 3.11 2>/dev/null || true)
  fi
  if [ -n "$py" ] && [ -x "$py" ]; then
    ln -sf "$py" /usr/local/bin/python3
  fi
  hash -r
  ok
}
ok && exit 0
export PATH="/usr/local/bin:/root/.local/bin:$PATH"
uv_py && exit 0
if command -v apk >/dev/null 2>&1; then
  apk add --no-cache python3 py3-pip py3-virtualenv curl ca-certificates || true
fi
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update || true
  apt-get install -y python3.12 python3.12-venv python3.12-dev python3-pip curl ca-certificates \
    || apt-get install -y python3.11 python3.11-venv python3.11-dev python3-pip curl ca-certificates \
    || apt-get install -y python3-pip curl ca-certificates \
    || true
fi
if command -v python3.12 >/dev/null 2>&1; then
  ln -sf "$(command -v python3.12)" /usr/local/bin/python3
elif command -v python3.11 >/dev/null 2>&1; then
  ln -sf "$(command -v python3.11)" /usr/local/bin/python3
fi
hash -r
ok && exit 0
python3 -m pip install --quiet uv==0.9.5 || python3 -m pip install --quiet --user uv==0.9.5 || true
hash -r
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh || true
  export PATH="/usr/local/bin:/root/.local/bin:$PATH"
  hash -r
fi
uv_py
python3 -V >&2
ok
""".strip()
_UV_SEED = r"""
set -e
mkdir -p /root/.local/bin
cp /installed-agent/steerable/venv/bin/uv /root/.local/bin/uv
if [ -x /installed-agent/steerable/venv/bin/uvx ]; then
  cp /installed-agent/steerable/venv/bin/uvx /root/.local/bin/uvx
else
  ln -sf uv /root/.local/bin/uvx
fi
chmod +x /root/.local/bin/uv /root/.local/bin/uvx
# Ubuntu 24.04 images are Python 3.12. TB test.sh still passes
# `uvx -p 3.13`, which downloads python-build-standalone from GitHub
# (~32 MB) and often times out under Clash before pytest. GHA has no
# proxy and keeps the requested 3.13 interpreter.
if [ -n "${HTTP_PROXY}${HTTPS_PROXY}${http_proxy}${https_proxy}" ]; then
  mv /root/.local/bin/uvx /root/.local/bin/uvx.real
  cat > /root/.local/bin/uvx << 'EOF'
#!/bin/bash
args=()
pending=0
for arg in "$@"; do
  if [[ $pending -eq 1 ]]; then
    pending=0
    if [[ "$arg" == 3.13 || "$arg" == 3.13.* ]]; then
      arg="$(command -v python3)"
    fi
  elif [[ "$arg" == "-p" || "$arg" == "--python" ]]; then
    pending=1
  fi
  args+=("$arg")
done
exec /root/.local/bin/uvx.real "${args[@]}"
EOF
  chmod +x /root/.local/bin/uvx
fi
cat > /root/.local/bin/env << 'EOF'
#!/bin/sh
case ":${PATH}:" in
    *:"$HOME/.local/bin":*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
# Local Clash often stalls pypi.org; GHA has no proxy and should keep PyPI.
if [ -n "${HTTP_PROXY}${HTTPS_PROXY}${http_proxy}${https_proxy}" ]; then
  export UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
  export UV_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
fi
EOF
chmod +x /root/.local/bin/env
# TB test.sh always runs `curl … astral.sh/uv/…/install.sh | sh`. Prefer this
# stub over /usr/bin/curl so the GitHub tarball GET never starts.
cat > /usr/local/bin/curl << 'EOF'
#!/bin/sh
url=""
for arg in "$@"; do
  case "$arg" in
    http://*|https://*) url=$arg ;;
  esac
done
case "$url" in
  *astral.sh/uv*|*github.com/astral-sh/uv*|*githubusercontent.com*uv-*)
    printf '%s\n' '#!/bin/sh' 'exit 0'
    exit 0
    ;;
esac
if [ -x /usr/bin/curl ]; then
  exec /usr/bin/curl "$@"
fi
echo "curl: not installed" >&2
exit 1
EOF
chmod +x /usr/local/bin/curl
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
# TB test.sh often runs `uvx -p 3.13`. Prefetch so the verifier is not a
# 30-minute GitHub GET (mcmc-sampling-stan VerifierTimeoutError).
i=0
while [ "$i" -lt 3 ]; do
  /root/.local/bin/uv python install 3.13 && break
  i=$((i+1))
  sleep 5
done
/root/.local/bin/uv --version
""".strip()
_UV_PIP_INSTALL = (
    f"{_REMOTE_SRC}/venv/bin/pip install --quiet "
    "-i https://pypi.tuna.tsinghua.edu.cn/simple "
    "--trusted-host pypi.tuna.tsinghua.edu.cn uv==0.9.5"
)
_NO_PROXY_ENV = {
    "HTTP_PROXY": "",
    "HTTPS_PROXY": "",
    "http_proxy": "",
    "https_proxy": "",
    "ALL_PROXY": "",
    "all_proxy": "",
}


def pip_install_command(
    remote_pkgs: list[str], extra_args: tuple[str, ...] = ()
) -> str:
    pip = f"{_REMOTE_SRC}/venv/bin/pip"
    quoted = " ".join(shlex.quote(part) for part in (*extra_args, *remote_pkgs))
    return f"{shlex.quote(pip)} install --quiet {quoted}"


def venv_tarball(py_tag: str) -> Path:
    return _VENV_CACHE_DIR / f"steerable-venv-cp{py_tag}-linux-amd64.tgz"


def uv_tarball() -> Path | None:
    path = _VENV_CACHE_DIR / _UV_CACHE_NAME
    if path.is_file() and path.stat().st_size >= _UV_MIN_BYTES:
        return path
    return None


_UV_MUSL_URL = (
    "https://github.com/astral-sh/uv/releases/download/0.9.5/"
    "uv-x86_64-unknown-linux-musl.tar.gz"
)
_UV_MUSL_BIN = "uv-x86_64-unknown-linux-musl"
_UV_MUSL_MIN_BYTES = 1_000_000


def musl_uv_binary(*, fetch: bool = False) -> Path | None:
    """Linux musl-static uv for Debian 11 / Alpine trial images.

    Downloaded on the Harbor host (GHA has GitHub). ``fetch=False`` is for
    tests and never touches the network.
    """
    dest = _VENV_CACHE_DIR / _UV_MUSL_BIN
    if dest.is_file() and dest.stat().st_size >= _UV_MUSL_MIN_BYTES:
        return dest
    if not fetch:
        return None
    _VENV_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tar_path = _VENV_CACHE_DIR / f"{_UV_MUSL_BIN}.tar.gz"
    try:
        import tarfile
        import urllib.request

        urllib.request.urlretrieve(_UV_MUSL_URL, tar_path)
        with tarfile.open(tar_path, "r:gz") as tf:
            member = next(
                (
                    m
                    for m in tf.getmembers()
                    if m.isfile()
                    and (m.name.endswith("/uv") or m.name == "uv")
                ),
                None,
            )
            if member is None:
                return None
            extracted = tf.extractfile(member)
            if extracted is None:
                return None
            dest.write_bytes(extracted.read())
        dest.chmod(0o755)
    except OSError:
        return None
    if dest.is_file() and dest.stat().st_size >= _UV_MUSL_MIN_BYTES:
        return dest
    return None


def rewrite_loopback_host(value: str) -> str:
    """Docker Desktop: host Clash on 127.0.0.1 is not the container loopback."""
    return value.replace("127.0.0.1", "host.docker.internal").replace(
        "localhost", "host.docker.internal"
    )


def rewrite_forwarded_env_value(key: str, value: str) -> str:
    """Rewrite proxy URLs for Docker Desktop; leave NO_PROXY host lists intact."""
    if key.lower() == "no_proxy":
        return value
    if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
        return rewrite_loopback_host(value)
    return value


def ensure_github_no_proxy(env: dict[str, str]) -> None:
    """Clash GET of the GitHub uv tarball often stalls; apt still uses the proxy.

    Trial-local HTTP (nginx, git webserver hidden tests) must also bypass Clash,
    otherwise localhost:8080 is intercepted and returns a host app page.
    """
    extra = (
        "localhost",
        "127.0.0.1",
        "::1",
        "github.com",
        "astral.sh",
        "releases.astral.sh",
        "objects.githubusercontent.com",
        "raw.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "codeload.github.com",
    )
    parts: list[str] = []
    seen: set[str] = set()
    for raw in (env.get("NO_PROXY"), env.get("no_proxy"), *extra):
        if not raw:
            continue
        for item in str(raw).split(","):
            host = item.strip()
            key = host.lower()
            if not host or key in seen:
                continue
            seen.add(key)
            parts.append(host)
    merged = ",".join(parts)
    env["NO_PROXY"] = merged
    env["no_proxy"] = merged


# Prefer the interpreter _ENSURE_PYTHON_310 installs. Harbor environment.exec
# may ignore _persistent_env PATH, so `python3 -m venv` would still hit
# /usr/bin/python3 3.9.2 on qemu-alpine-ssh.
_PY310_BIN = 'p=/usr/local/bin/python3; [ -x "$p" ] || p=python3'


def trial_python_ok() -> str:
    return (
        f"{_PY310_BIN}; $p -c 'import sys; raise SystemExit("
        "0 if sys.version_info >= (3, 10) else 1)'"
    )


def trial_python_tag() -> str:
    return (
        f"{_PY310_BIN}; $p -c 'import sys; print(\"%s%s\" % "
        "(sys.version_info.major, sys.version_info.minor))'"
    )


def trial_python_venv(venv: str) -> str:
    return (
        f"{_PY310_BIN}; $p -c 'import sys; raise SystemExit("
        "0 if sys.version_info >= (3, 10) else 1)' && "
        f"$p -m venv {shlex.quote(venv)}"
    )


_DEFAULT_TRIAL_PATH = (
    "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin"
)
_TRIAL_PATH_EXTRAS = (
    "/root/.local/bin",
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
)


def merge_trial_path(existing: str) -> str:
    """Keep uv and sbin on PATH so hidden tests that call ``which`` succeed.

    ``nginx-request-logging`` installs nginx to ``/usr/sbin``; verifier
    ``which nginx`` fails when PATH is only ``/usr/bin``.
    """
    parts = [p for p in (existing or "").split(":") if p]
    if not parts:
        return _DEFAULT_TRIAL_PATH
    have = set(parts)
    prefix = [p for p in _TRIAL_PATH_EXTRAS if p not in have]
    return ":".join([*prefix, *parts])
