"""Pure Harbor adapter helpers (no Harbor import; safe for unit tests)."""

from __future__ import annotations

import json
import shlex
import tempfile
from pathlib import Path


def python_tag_supported(tag: str) -> bool:
    """The agent packages require Python >= 3.10 (match the pyproject floor)."""
    try:
        major, minor = int(tag[:1]), int(tag[1:])
    except (ValueError, IndexError):
        return True  # unparsable tag: let pip report the real requirement
    return (major, minor) >= (3, 10)

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


def spec_as_json(spec_path: str | Path) -> Path:
    """Convert a host-side YAML/JSON harness spec to a JSON temp file.

    The trial container installs only the workspace wheels — PyYAML is
    deliberately not a runtime dependency — so the spec crosses the
    boundary as JSON, which the runtime loader parses with stdlib json.
    The conversion happens on the host, where PyYAML is available (the
    evals venv already parses suite.yaml with it). The returned file
    persists until process exit so the upload can read it later.
    """
    source = Path(spec_path)
    if source.suffix.lower() == ".json":
        return source
    import yaml  # host-side only; the container never sees PyYAML

    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="steerable-harness-", delete=False, encoding="utf-8"
    )
    with tmp:
        json.dump(data, tmp)
    return Path(tmp.name)


def uv_tarball() -> Path | None:
    path = _VENV_CACHE_DIR / _UV_CACHE_NAME
    if path.is_file() and path.stat().st_size >= _UV_MIN_BYTES:
        return path
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


_DEFAULT_TRIAL_PATH = (
    "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin"
)
_TRIAL_PATH_EXTRAS = (
    "/root/.local/bin",
    "/usr/local/sbin",
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
