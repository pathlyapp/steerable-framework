from __future__ import annotations

from pathlib import Path

from evals.harbor_helpers import (
    _APT_PYTHON_INSTALL,
    _ENSURE_PYTHON_310,
    _UV_MIN_BYTES,
    _UV_PIP_INSTALL,
    _UV_SEED,
    ensure_github_no_proxy,
    merge_trial_path,
    pip_install_command,
    rewrite_forwarded_env_value,
    rewrite_loopback_host,
    uv_tarball,
    venv_tarball,
)


def test_venv_tarball_is_abi_specific() -> None:
    path = venv_tarball("313")
    assert path.name == "steerable-venv-cp313-linux-amd64.tgz"


def test_overlay_pip_install_is_no_deps() -> None:
    cmd = pip_install_command(
        ["/installed-agent/steerable/sidecar"],
        extra_args=("--no-deps", "--upgrade", "--force-reinstall"),
    )
    assert "--no-deps" in cmd
    assert "--force-reinstall" in cmd
    assert "/installed-agent/steerable/venv/bin/pip" in cmd
    assert "sidecar" in cmd


def test_full_pip_install_resolves_deps() -> None:
    cmd = pip_install_command(["/installed-agent/steerable/sidecar"])
    assert "--no-deps" not in cmd


def test_loopback_proxy_rewrites_to_host_docker() -> None:
    assert (
        rewrite_loopback_host("http://127.0.0.1:7890")
        == "http://host.docker.internal:7890"
    )


def test_github_hosts_join_no_proxy() -> None:
    env = {"HTTP_PROXY": "http://host.docker.internal:7890"}
    ensure_github_no_proxy(env)
    assert "github.com" in env["NO_PROXY"]
    assert "astral.sh" in env["NO_PROXY"]
    assert "releases.astral.sh" in env["NO_PROXY"]
    for host in ("localhost", "127.0.0.1", "::1"):
        assert host in env["NO_PROXY"].split(",")
    assert env["NO_PROXY"] == env["no_proxy"]


def test_no_proxy_localhost_is_not_rewritten_to_host_docker() -> None:
    assert (
        rewrite_forwarded_env_value("NO_PROXY", "localhost,127.0.0.1")
        == "localhost,127.0.0.1"
    )
    assert (
        rewrite_forwarded_env_value("HTTP_PROXY", "http://127.0.0.1:7890")
        == "http://host.docker.internal:7890"
    )


def test_apt_python_install_waits_without_fuser() -> None:
    assert "fuser" not in _APT_PYTHON_INSTALL
    assert "readlink" in _APT_PYTHON_INSTALL
    assert "python3-venv" in _APT_PYTHON_INSTALL
    assert "/usr/bin/apt-get" in _APT_PYTHON_INSTALL
    assert "mirrors.tuna.tsinghua.edu.cn/ubuntu" in _APT_PYTHON_INSTALL
    assert "archive.ubuntu.com" in _APT_PYTHON_INSTALL


def test_ensure_python_310_upgrades_before_venv() -> None:
    assert "sys.version_info >= (3, 10)" in _ENSURE_PYTHON_310
    assert "python3.12" in _ENSURE_PYTHON_310
    assert "apk add" in _ENSURE_PYTHON_310
    assert "uv python install 3.12" in _ENSURE_PYTHON_310
    assert 'PATH="/usr/local/bin:' in _ENSURE_PYTHON_310
    assert "hash -r" in _ENSURE_PYTHON_310
    assert "ca-certificates" in _ENSURE_PYTHON_310
    assert "python3-pip" in _ENSURE_PYTHON_310


def test_uv_tarball_missing_skips_seed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("evals.harbor_helpers._VENV_CACHE_DIR", tmp_path)
    assert uv_tarball() is None


def test_uv_seed_installs_from_tsinghua_without_github() -> None:
    assert "pypi.tuna.tsinghua.edu.cn" in _UV_PIP_INSTALL
    assert "uv==0.9.5" in _UV_PIP_INSTALL
    assert "github.com" not in _UV_PIP_INSTALL
    assert "/root/.local/bin/uv" in _UV_SEED
    assert "/usr/local/bin/curl" in _UV_SEED
    assert "astral.sh/uv" in _UV_SEED
    assert "exec /usr/bin/curl" in _UV_SEED
    assert "pypi.tuna.tsinghua.edu.cn" in _UV_SEED
    assert "UV_INDEX_URL" in _UV_SEED
    assert "HTTP_PROXY" in _UV_SEED
    assert "uvx.real" in _UV_SEED
    assert '"$arg" == 3.13' in _UV_SEED
    assert "/root/.local/bin/uv python install 3.13" in _UV_SEED
    assert "UV_PYTHON_INSTALL_DIR" in _UV_SEED


def test_uv_tarball_rejects_truncated_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("evals.harbor_helpers._VENV_CACHE_DIR", tmp_path)
    truncated = tmp_path / "uv-0.9.5-x86_64-unknown-linux-gnu.tar.gz"
    truncated.write_bytes(b"\0" * 2_000_000)
    assert truncated.stat().st_size < _UV_MIN_BYTES
    assert uv_tarball() is None


def test_merge_trial_path_adds_sbin_and_uv() -> None:
    merged = merge_trial_path("/usr/bin:/bin")
    assert merged.startswith("/root/.local/bin")
    assert "/usr/local/bin" in merged.split(":")
    assert "/usr/sbin" in merged.split(":")
    assert merged.endswith("/usr/bin:/bin")
    empty = merge_trial_path("")
    assert "/usr/sbin" in empty.split(":")
    assert "/usr/bin" in empty.split(":")
    already = merge_trial_path("/usr/sbin:/usr/bin")
    assert already.split(":").count("/usr/sbin") == 1


def test_harbor_run_matches_claude_code_tb_knobs() -> None:
    src = Path(__file__).resolve().parents[1] / "harbor_steerable.py"
    text = src.read_text()
    assert 'STEERABLE_REASONING_EFFORT", "max"' in text
    assert 'STEERABLE_TEMPERATURE", "1.0"' in text
    assert 'STEERABLE_MAX_TOKENS", "65536"' in text
    assert 'STEERABLE_SOFT_TIMEOUT_MS", "10200000"' in text
    assert 'STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC", "1800"' in text
    assert "--max-rounds 160" in text
    assert text.index("_ensure_python_310") < text.index("_python_tag")
    assert text.index("_merge_trial_path") < text.index("_python_tag")
    assert "trial python is still <3.10" in text
