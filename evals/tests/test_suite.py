from __future__ import annotations

from pathlib import Path

import pytest

from evals.suite import (
    EXCLUSIVE_PACK_TASKS,
    LIVE_AGENTS,
    PINNED_HARBOR_VERSION,
    PRODUCT_AGENT,
    STEERABLE_IMPORT_PATH,
    SUITE_PATH,
    SuiteError,
    agent_ready,
    dataset_org,
    harbor_argv,
    harbor_task_name,
    load_suite,
    missing_env,
    resolve_tasks,
    shard_tasks,
)

CHEAP_12 = (
    "fix-git",
    "openssl-selfsigned-cert",
    "sqlite-db-truncate",
    "nginx-request-logging",
    "configure-git-webserver",
    "sanitize-git-repo",
    "polyglot-c-py",
    "log-summary-date-ranges",
    "filter-js-from-html",
    "password-recovery",
    "git-multibranch",
    "sqlite-with-gcov",
)

FAILED_PREV = (
    "dna-assembly",
    "extract-moves-from-video",
    "filter-js-from-html",
    "gcode-to-text",
    "gpt2-codegolf",
    "largest-eigenval",
    "make-doom-for-mips",
    "make-mips-interpreter",
    "model-extraction-relu-logits",
    "mteb-retrieve",
    "path-tracing-reverse",
    "protein-assembly",
    "regex-chess",
    "rstan-to-pystan",
    "train-fasttext",
)


def test_suite_yaml_exists() -> None:
    assert SUITE_PATH.is_file()


def test_catalog_is_89_unique_ids() -> None:
    suite = load_suite()
    assert len(suite.catalog) == 89
    assert len(suite.catalog_set) == 89


def test_catalog_minutes_cover_every_catalog_id() -> None:
    suite = load_suite()
    assert set(suite.catalog_minutes) == suite.catalog_set
    assert all(value >= 1 for value in suite.catalog_minutes.values())
    assert suite.pack_floor_minutes == 180


def test_cheap_12_is_pinned_subset() -> None:
    suite = load_suite()
    assert suite.splits["cheap-12"] == CHEAP_12
    assert set(CHEAP_12) <= suite.catalog_set


def test_failed_prev_is_pinned_catalog_subset() -> None:
    suite = load_suite()
    assert suite.splits["failed-prev"] == FAILED_PREV
    assert len(FAILED_PREV) == 15
    assert set(FAILED_PREV) <= suite.catalog_set
    assert FAILED_PREV == tuple(task for task in suite.catalog if task in set(FAILED_PREV))


def test_oracle_canary_is_in_cheap_12() -> None:
    suite = load_suite()
    canary = suite.splits["oracle-canary"]
    assert canary == ("fix-git",)
    assert set(canary) <= set(suite.splits["cheap-12"])


def test_dsh_is_skipped() -> None:
    suite = load_suite()
    dsh = suite.agents["dsh"]
    assert dsh.skipped is True
    assert dsh.harbor is None
    assert "Harbor" in (dsh.reason or "")


def test_pi_is_first_party_harbor_agent() -> None:
    suite = load_suite()
    pi = suite.agents["pi"]
    assert pi.skipped is False
    assert pi.harbor == "pi"
    assert pi.model == "anthropic/claude-sonnet-4-5"
    assert pi.env_any == ("ANTHROPIC_API_KEY",)


def test_live_agents_include_product() -> None:
    suite = load_suite()
    assert LIVE_AGENTS == ("claude-code", "codex", "pi", PRODUCT_AGENT)
    for name in ("claude-code", "codex", "pi"):
        spec = suite.agents[name]
        assert spec.skipped is False
        assert spec.harbor == name
        assert spec.model
    product = suite.agents[PRODUCT_AGENT]
    assert product.skipped is False
    assert product.harbor == STEERABLE_IMPORT_PATH
    assert product.model == "openai/z-ai/glm-5.3-flash"


def test_setup_harbor_action_matches_pin() -> None:
    action = Path(__file__).resolve().parents[2] / ".github" / "actions" / "setup-harbor" / "action.yml"
    text = action.read_text()
    assert f'default: "{PINNED_HARBOR_VERSION}"' in text
    assert "uv-x86_64-unknown-linux-musl" in text
    assert "uv-x86_64-unknown-linux-musl.tar.gz" in text
    assert "find /tmp -name uv" not in text
    assert 'find "$extract" -name uv' in text
    assert "cpython-3.12-linux-x86_64-gnu.tgz" in text
    assert "uv python install 3.12" in text


def test_gha_forwards_steerable_gateway_not_official_openai() -> None:
    root = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    weekly = (root / "evals-weekly.yml").read_text()
    oracle = (root / "evals-oracle.yml").read_text()
    assert "STEERABLE_API_KEY: ${{ secrets.STEERABLE_API_KEY }}" in weekly
    assert "STEERABLE_BASE_URL: ${{ secrets.STEERABLE_BASE_URL }}" in weekly
    assert "STEERABLE_API_KEY: ${{ secrets.STEERABLE_API_KEY }}" in oracle
    assert "STEERABLE_BASE_URL: ${{ secrets.STEERABLE_BASE_URL }}" in oracle
    steerable_job = oracle.split("name: Harbor product canary", 1)[1]
    assert "OPENAI_API_KEY" not in steerable_job.split("upload-artifact", 1)[0]
    assert "set STEERABLE_API_KEY + STEERABLE_BASE_URL for the product agent" in weekly
    assert "FEISHU_BOT_WEBHOOK" in weekly
    assert "python3 -m evals.feishu" in weekly
    assert "python3 -m evals.feishu" in oracle
    assert "if: ${{ !cancelled() }}" in oracle
    assert "if: ${{ !cancelled() }}" in weekly
    assert "merge-multiple: true" not in oracle
    assert "merge-multiple: true" not in weekly
    assert "--n-concurrent 2" in weekly
    assert "**/eval-status-*.txt" in weekly
    assert "evals/jobs/steerable/*/*/result.json" in weekly
    assert weekly.count("evals/jobs/steerable/*/*/result.json") == 2
    assert "pull_request:" not in weekly
    assert "--split catalog" in weekly
    assert "--split failed-prev" in weekly
    assert "--shards 16" not in weekly
    assert weekly.count("--shards 24") == 1
    assert "--shards 32" not in weekly
    assert "--shards 36" not in weekly
    assert "--shards 48" not in weekly
    assert "--shards 49" in weekly
    assert "--shards 8" not in weekly
    assert "--shards 4 " not in weekly
    assert "--shards 4\n" not in weekly
    assert (
        "shard: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, "
        "16, 17, 18, 19, 20, 21, 22, 23]"
    ) in weekly
    assert weekly.count(
        "shard: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, "
        "16, 17, 18, 19, 20, 21, 22, 23]"
    ) == 1
    assert (
        "shard: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, "
        "16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, "
        "32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48]"
    ) in weekly
    assert "timeout-minutes: 360" in weekly
    assert weekly.count("--agent-timeout-multiplier 12") == 2
    assert weekly.count("--verifier-timeout-multiplier 2") == 2
    cheap_job = weekly.split("  failed-prev:", 1)[0]
    assert "--agent-timeout-multiplier 3" in cheap_job
    assert "--agent-timeout-multiplier 12" not in cheap_job
    assert "--verifier-timeout-multiplier" not in cheap_job
    assert "options:" in weekly
    assert "- failed-prev" in weekly
    assert "github.event.inputs.split == 'cheap-12'" in weekly
    assert "github.event.inputs.split != 'catalog'" not in weekly
    catalog_job = weekly.split("name: Harbor catalog shard", 1)[1]
    assert "OPENAI_API_KEY" not in catalog_job.split("upload-artifact", 1)[0]
    failed_job = weekly.split("name: Harbor failed-prev shard", 1)[1]
    assert '--split failed-prev --shard "${{ matrix.shard }}" --shards 24' in weekly
    assert '--split catalog --shard "${{ matrix.shard }}" --shards 49' in weekly
    assert "OPENAI_API_KEY" not in failed_job.split("upload-artifact", 1)[0]
    assert "STEERABLE_API_KEY: ${{ secrets.STEERABLE_API_KEY }}" in catalog_job
    assert "agent/headless.log" in weekly
    assert "verifier/test-stdout.txt" in weekly
    assert "agent/headless.log" in oracle


def test_harbor_task_name_prefixes_org() -> None:
    assert harbor_task_name("terminal-bench/terminal-bench-2-1", "fix-git") == (
        "terminal-bench/fix-git"
    )
    assert harbor_task_name(
        "terminal-bench/terminal-bench-2-1", "terminal-bench/fix-git"
    ) == "terminal-bench/fix-git"
    with pytest.raises(SuiteError, match="org/name"):
        dataset_org("not-an-org")


def test_oracle_needs_no_key() -> None:
    suite = load_suite()
    oracle = suite.agents["oracle"]
    assert agent_ready(oracle, {}) is True
    assert missing_env(oracle, {}) == ()


def test_claude_code_and_pi_need_anthropic_key() -> None:
    suite = load_suite()
    empty: dict[str, str] = {}
    keyed = {"ANTHROPIC_API_KEY": "sk-test"}
    for name in ("claude-code", "pi"):
        spec = suite.agents[name]
        assert agent_ready(spec, empty) is False
        assert missing_env(spec, empty) == ("ANTHROPIC_API_KEY",)
        assert agent_ready(spec, keyed) is True


def test_steerable_accepts_any_listed_key() -> None:
    suite = load_suite()
    spec = suite.agents["steerable"]
    assert agent_ready(spec, {}) is False
    assert agent_ready(spec, {"OPENAI_API_KEY": "sk"}) is True
    assert agent_ready(spec, {"ANTHROPIC_API_KEY": "sk"}) is True
    assert agent_ready(spec, {"STEERABLE_API_KEY": "sk"}) is True


def test_codex_accepts_either_openai_or_codex_key() -> None:
    suite = load_suite()
    spec = suite.agents["codex"]
    assert agent_ready(spec, {}) is False
    assert agent_ready(spec, {"OPENAI_API_KEY": "sk-openai"}) is True
    assert agent_ready(spec, {"CODEX_API_KEY": "sk-codex"}) is True
    assert missing_env(spec, {}) == ("OPENAI_API_KEY", "CODEX_API_KEY")


def test_resolve_tasks_override_must_be_in_catalog() -> None:
    suite = load_suite()
    assert resolve_tasks(suite, "cheap-12", ["fix-git"]) == ("fix-git",)
    with pytest.raises(SuiteError, match="not in catalog"):
        resolve_tasks(suite, "cheap-12", ["not-a-task"])
    with pytest.raises(SuiteError, match="unknown split"):
        resolve_tasks(suite, "not-a-split")


def test_shard_tasks_covers_catalog_without_overlap() -> None:
    suite = load_suite()
    shards = [
        shard_tasks(suite.catalog, shard=i, shards=8, minutes=suite.catalog_minutes)
        for i in range(8)
    ]
    flat = [task for shard in shards for task in shard]
    assert len(flat) == 89
    assert sorted(flat) == sorted(suite.catalog)
    assert all(10 <= len(shard) <= 12 for shard in shards)
    loads = [sum(suite.catalog_minutes[task] for task in shard) for shard in shards]
    assert max(loads) - min(loads) <= 2
    round_robin = [
        shard_tasks(suite.catalog, shard=i, shards=8) for i in range(8)
    ]
    rr_loads = [sum(suite.catalog_minutes[task] for task in shard) for shard in round_robin]
    assert max(loads) < max(rr_loads)
    with pytest.raises(SuiteError, match="out of range"):
        shard_tasks(suite.catalog, shard=8, shards=8)


def test_pack_floor_keeps_catalog_shards_inside_gha_wall() -> None:
    """Harbor ×12 wrap is 180 min. n-concurrent=2, 360-minute GHA cap.

    24 catalog shards still pack 4 tasks (2×180 min waves = 360 min exact).
    36 shards still pack 3 (one leftover 180-min wave = 360 min exact).
    49 shards pack ≤2 after eight exclusive MIPS/QEMU/Windows/video/SQL/FastText tasks.
    """
    suite = load_suite()
    catalog = [
        shard_tasks(
            suite.catalog,
            shard=i,
            shards=49,
            minutes=suite.catalog_minutes,
            pack_floor=suite.pack_floor_minutes,
        )
        for i in range(49)
    ]
    flat = [task for shard in catalog for task in shard]
    assert len(flat) == 89
    assert sorted(flat) == sorted(suite.catalog)
    assert max(len(shard) for shard in catalog) <= 2
    failed = [
        shard_tasks(
            suite.splits["failed-prev"],
            shard=i,
            shards=24,
            minutes=suite.catalog_minutes,
            pack_floor=suite.pack_floor_minutes,
        )
        for i in range(24)
    ]
    assert sum(len(shard) for shard in failed) == 15
    assert max(len(shard) for shard in failed) <= 2
    for packed in (catalog, failed):
        for bucket in packed:
            if EXCLUSIVE_PACK_TASKS.intersection(bucket):
                assert len(bucket) == 1, bucket


def test_shard_tasks_round_robin_without_minutes() -> None:
    tasks = ("a", "b", "c", "d")
    assert shard_tasks(tasks, shard=0, shards=2) == ("a", "c")
    assert shard_tasks(tasks, shard=1, shards=2) == ("b", "d")


def test_harbor_argv_oracle_omits_model() -> None:
    suite = load_suite()
    argv = harbor_argv(
        suite,
        agent="oracle",
        tasks=["fix-git"],
        jobs_dir=Path("evals/jobs/oracle"),
    )
    assert argv[0] == "harbor"
    assert argv[1:4] == ["run", "--dataset", "terminal-bench/terminal-bench-2-1"]
    assert "--agent" in argv
    assert argv[argv.index("--agent") + 1] == "oracle"
    assert "--model" not in argv
    assert argv[argv.index("--include-task-name") + 1] == "terminal-bench/fix-git"
    assert "--yes" in argv


def test_harbor_argv_pi_uses_include_task_name() -> None:
    suite = load_suite()
    argv = harbor_argv(
        suite,
        agent="pi",
        tasks=CHEAP_12,
        jobs_dir=Path("evals/jobs/pi"),
    )
    assert argv[argv.index("--agent") + 1] == "pi"
    assert argv[argv.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
    includes = [
        argv[i + 1]
        for i, flag in enumerate(argv)
        if flag == "--include-task-name"
    ]
    assert includes == [f"terminal-bench/{task}" for task in CHEAP_12]


def test_harbor_argv_steerable_uses_import_path() -> None:
    suite = load_suite()
    argv = harbor_argv(
        suite,
        agent="steerable",
        tasks=["fix-git"],
        jobs_dir=Path("evals/jobs/steerable"),
        agent_setup_timeout_multiplier=3,
        environment_build_timeout_multiplier=3,
        agent_timeout_multiplier=3,
        verifier_timeout_multiplier=2,
    )
    assert argv[argv.index("--agent") + 1] == STEERABLE_IMPORT_PATH
    assert argv[argv.index("--model") + 1] == "openai/z-ai/glm-5.3-flash"
    assert argv[argv.index("--include-task-name") + 1] == "terminal-bench/fix-git"
    assert argv[argv.index("--agent-setup-timeout-multiplier") + 1] == "3"
    assert argv[argv.index("--environment-build-timeout-multiplier") + 1] == "3"
    assert argv[argv.index("--agent-timeout-multiplier") + 1] == "3"
    assert argv[argv.index("--verifier-timeout-multiplier") + 1] == "2"


def test_harbor_argv_rejects_dsh() -> None:
    suite = load_suite()
    with pytest.raises(SuiteError, match="cannot run Harbor"):
        harbor_argv(
            suite,
            agent="dsh",
            tasks=["fix-git"],
            jobs_dir=Path("evals/jobs/dsh"),
        )
