from __future__ import annotations

from pathlib import Path

from evals.suite import load_suite

from evals.harbor_helpers import (
    _APT_PYTHON_INSTALL,
    _ENSURE_PYTHON_310,
    _PY310_BIN,
    _UV_MIN_BYTES,
    _UV_PIP_INSTALL,
    _UV_SEED,
    ensure_github_no_proxy,
    is_zai_glm,
    merge_trial_path,
    pip_install_command,
    rewrite_forwarded_env_value,
    rewrite_loopback_host,
    spec_as_json,
    trial_python_ok,
    trial_python_tag,
    trial_python_venv,
    usage_from_headless_log,
    uv_tarball,
    uv_tarball,
    musl_uv_binary,
    linux_cpython_tarball,
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
    assert "astral.sh/uv/0.9.5/install.sh" in _ENSURE_PYTHON_310
    assert "pin_usr_bin" not in _ENSURE_PYTHON_310
    assert "/usr/bin/python3.9" not in _ENSURE_PYTHON_310
    assert "ln -sf /usr/local/bin/python3 /usr/bin/python3" not in _ENSURE_PYTHON_310
    assert "/usr/local/bin/python3" in _ENSURE_PYTHON_310
    assert "uv_py()" in _ENSURE_PYTHON_310
    assert _ENSURE_PYTHON_310.index("uv_py && exit 0") < _ENSURE_PYTHON_310.index(
        "pip install"
    )
    assert 'while [ "$i" -lt 3 ]' in _ENSURE_PYTHON_310
    assert "uv python find 3.12" in _ENSURE_PYTHON_310
    assert "uv python install 3.11" in _ENSURE_PYTHON_310


def test_uv_tarball_missing_skips_seed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("evals.harbor_helpers._VENV_CACHE_DIR", tmp_path)
    assert uv_tarball() is None


def test_musl_uv_binary_skips_network_without_fetch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("evals.harbor_helpers._VENV_CACHE_DIR", tmp_path)
    assert musl_uv_binary(fetch=False) is None


def test_musl_uv_binary_uses_cached_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("evals.harbor_helpers._VENV_CACHE_DIR", tmp_path)
    cached = tmp_path / "uv-x86_64-unknown-linux-musl"
    cached.write_bytes(b"u" * 1_000_001)
    assert musl_uv_binary(fetch=False) == cached


def test_linux_cpython_tarball_skips_network_without_fetch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("evals.harbor_helpers._VENV_CACHE_DIR", tmp_path)
    assert linux_cpython_tarball(fetch=False) is None


def test_linux_cpython_tarball_uses_cached_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("evals.harbor_helpers._VENV_CACHE_DIR", tmp_path)
    cached = tmp_path / "cpython-3.12-linux-x86_64-gnu.tgz"
    cached.write_bytes(b"c" * 5_000_001)
    assert linux_cpython_tarball(fetch=False) == cached


def test_linux_cpython_tarball_rejects_tiny_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("evals.harbor_helpers._VENV_CACHE_DIR", tmp_path)
    tiny = tmp_path / "cpython-3.12-linux-x86_64-gnu.tgz"
    tiny.write_bytes(b"c" * 100)
    assert linux_cpython_tarball(fetch=False) is None


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
    assert 'while [ "$i" -lt 3 ]' in _UV_SEED


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


def test_spec_as_json_converts_yaml_for_the_container(tmp_path) -> None:
    # Arm B failed live on 2026-08-31: the trial container has no PyYAML
    # (deliberately not a runtime dep), so a .yaml upload died in
    # load_harness_spec. The spec must cross as JSON.
    spec = tmp_path / "arm.harness.yaml"
    spec.write_text("tools: full\norchestration: single\nvalidator: \"null\"\n")
    converted = spec_as_json(spec)
    assert converted.suffix == ".json"
    import json

    assert json.loads(converted.read_text()) == {
        "tools": "full",
        "orchestration": "single",
        "validator": "null",
    }


def test_spec_as_json_passes_json_through(tmp_path) -> None:
    spec = tmp_path / "arm.harness.json"
    spec.write_text('{"tools": "full"}')
    assert spec_as_json(spec) == spec


def test_spec_as_json_resolves_repo_relative_path_from_another_cwd(
    tmp_path, monkeypatch
) -> None:
    """Arm B sets STEERABLE_HARNESS=evals/harnesses/self_critique.harness.yaml.
    Harbor's agent process is not guaranteed to keep cwd at the repo root."""
    monkeypatch.chdir(tmp_path)
    converted = spec_as_json("evals/harnesses/self_critique.harness.yaml")
    import json

    assert json.loads(converted.read_text())["validator"] == "self_critique"


def test_usage_from_headless_log_reads_the_last_summary() -> None:
    text = (
        "noise\n"
        'STEERABLE_RUN_SUMMARY {"input_tokens": 1, "output_tokens": 2, "cache_tokens": 0}\n'
        "more noise\n"
        'STEERABLE_RUN_SUMMARY {"input_tokens": 100, "output_tokens": 20, "cache_tokens": 7}\n'
    )
    assert usage_from_headless_log(text) == (100, 20, 7)
    assert usage_from_headless_log("no summary") == (None, None, None)
    assert usage_from_headless_log("STEERABLE_RUN_SUMMARY {") == (None, None, None)


def test_calibration_knobs_can_be_overridden_per_arm() -> None:
    """A `setdefault` default is unreachable unless its key is forwarded.

    `_forwarded_env` builds the dict from the listed keys and the `setdefault`
    block then fills the gaps, so a key absent from `_TUNING_KEYS` is pinned
    to its default no matter what the dispatch sets. Temperature sat in that
    state while trajectory spread was the thing under investigation.
    """
    src = Path(__file__).resolve().parents[1] / "harbor_steerable.py"
    text = src.read_text()
    block = text[text.index("_TUNING_KEYS = (") : text.index("_PROXY_KEYS = (")]
    forwarded = {
        line.strip().strip(',"')
        for line in block.splitlines()
        if not line.strip().startswith("#")
    }
    assert "STEERABLE_TEMPERATURE" in forwarded
    assert "STEERABLE_REASONING_EFFORT" in forwarded
    assert "STEERABLE_REMINDERS" in forwarded
    assert "STEERABLE_DELIVERY_VERIFY" in forwarded
    assert "STEERABLE_LIVELOCK_EMPTY_STREAK" in forwarded
    assert "STEERABLE_PROMPT_CC_ALIGN" in forwarded
    assert "STEERABLE_HARNESS" in forwarded
    assert "STEERABLE_REQUEST_RECORD_PATH" in forwarded
    run_body = text[text.index("    async def run(") :]
    assert run_body.index("_forwarded_env(") < run_body.index("env.setdefault(")


def test_zai_defaults_do_not_follow_a_model_switch() -> None:
    """`reasoning_effort=max` and the z-ai endpoint pin are GLM-only.

    Sending either to another vendor breaks every trial rather than one:
    the effort value is rejected, and asking OpenRouter for a non-GLM model
    on the z-ai provider with fallbacks off answers "No endpoints found".
    """
    assert is_zai_glm("z-ai/glm-5.3-flash")
    assert is_zai_glm("Z-AI/GLM-5.3")
    assert not is_zai_glm("openai/gpt-5.5")
    assert not is_zai_glm("anthropic/claude-sonnet-4-5")
    assert not is_zai_glm("")


def test_committed_steerable_model_still_takes_the_zai_path() -> None:
    """The 0.8202 baseline depends on those defaults being applied.

    They are reached through the model id now, so changing the committed
    model silently drops `reasoning_effort=max` along with the endpoint pin.
    """
    model = load_suite().agents["steerable"].model or ""
    _, _, name = model.partition("/")
    assert is_zai_glm(name), model


def test_harbor_run_matches_claude_code_tb_knobs() -> None:
    src = Path(__file__).resolve().parents[1] / "harbor_steerable.py"
    text = src.read_text()
    assert 'STEERABLE_REASONING_EFFORT", "max"' in text
    assert 'STEERABLE_TEMPERATURE", "1.0"' in text
    assert 'STEERABLE_MAX_TOKENS", "65536"' in text
    assert 'STEERABLE_SOFT_TIMEOUT_MS", "9000000"' in text
    assert 'STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC", "10200"' in text
    assert 'STEERABLE_RETRY_MAX_ATTEMPTS", "12"' in text
    assert 'STEERABLE_RETRY_BASE_DELAY_MS", "2000"' in text
    assert 'STEERABLE_RETRY_MAX_DELAY_MS", "120000"' in text
    assert 'STEERABLE_OPENROUTER_PROVIDER", "z-ai"' in text
    assert 'STEERABLE_OPENROUTER_ALLOW_FALLBACKS", "0"' in text
    assert 'STEERABLE_OPENROUTER_REQUIRE_PARAMETERS", "0"' in text
    assert "--max-rounds 250" in text
    # TB 2.1 is an offline contract and the container has egress for the LLM
    # gateway, so a reachable web_fetch would let a trial answer from outside
    # the environment under test.
    assert "--no-web-tools" in text
    run_fn = text[
        text.index("@with_prompt_template") : text.index("def _forwarded_env")
    ]
    assert run_fn.index("await self.exec_as_agent") < run_fn.index(
        "await self._align_verifier_python"
    )
    assert run_fn.index("await self._align_verifier_python") < run_fn.index(
        "await self._record_token_usage"
    )
    assert "finally:" in run_fn
    assert text.index("await self._inject_host_uv") < text.index(
        "await self._inject_host_python"
    )
    assert text.index("await self._inject_host_python") < text.index(
        "await self._ensure_python_310"
    )
    assert text.index("await self._ensure_python_310") < text.index(
        "await self._align_verifier_python"
    )
    assert "ln -sf \"$p\" /usr/local/bin/python" in text
    assert 'ln -sf "$b" /usr/local/bin/python' in text
    inject_fn = text[
        text.index("async def _inject_host_python") : text.index(
            "async def _ensure_python_310"
        )
    ]
    assert inject_fn.index("command=_trial_python_ok()") < inject_fn.index(
        "_linux_cpython_tarball(fetch=True)"
    )
    assert "if check.return_code == 0" in inject_fn
    assert "_linux_cpython_tarball(fetch=True)" in text
    assert "/opt/steerable-python" in text
    assert "import ssl, zlib, sys" in text
    assert "_musl_uv_binary(fetch=True)" in text
    assert text.index("await self._ensure_python_310") < text.index(
        "py_tag = await self._python_tag"
    )
    assert 'rm -f /usr/local/bin/python3' in text
    assert "trial python is still <3.10" in text
    assert "trial python cp" in text
    assert "/usr/local/bin/python3" in _PY310_BIN
    assert _PY310_BIN in trial_python_ok()
    assert _PY310_BIN in trial_python_tag()
    venv_cmd = trial_python_venv("/installed-agent/steerable/venv")
    assert "/usr/local/bin/uv" in venv_cmd
    assert "venv --python" in venv_cmd
    assert "--seed" in venv_cmd
    assert "$p -m venv" in venv_cmd
    assert "--without-pip" in venv_cmd
    assert "rm -rf /installed-agent/steerable/venv" in venv_cmd
    assert "/installed-agent/steerable/venv" in venv_cmd
