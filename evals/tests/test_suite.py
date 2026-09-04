from __future__ import annotations

from pathlib import Path

import pytest

from evals.suite import (
    EXCLUSIVE_PACK_TASKS,
    GLM_LEG_IMPORT_PATHS,
    LIVE_AGENTS,
    PINNED_HARBOR_VERSION,
    PRODUCT_AGENT,
    PI_GLM_IMPORT_PATH,
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
    "adaptive-rejection-sampler",
    "build-pov-ray",
    "caffe-cifar-10",
    "circuit-fibsqrt",
    "cobol-modernization",
    "db-wal-recovery",
    "dna-assembly",
    "extract-moves-from-video",
    "filter-js-from-html",
    "gcode-to-text",
    "gpt2-codegolf",
    "headless-terminal",
    "install-windows-3.11",
    "largest-eigenval",
    "make-doom-for-mips",
    "make-mips-interpreter",
    "mteb-retrieve",
    "path-tracing",
    "path-tracing-reverse",
    "protein-assembly",
    "qemu-startup",
    "raman-fitting",
    "regex-chess",
    "rstan-to-pystan",
    "sam-cell-seg",
    "sanitize-git-repo",
    "schemelike-metacircular-eval",
    "torch-pipeline-parallelism",
    "train-fasttext",
    "video-processing",
    "winning-avg-corewars",
)


def test_suite_yaml_exists() -> None:
    assert SUITE_PATH.is_file()


def test_harnesses_parse_and_specs_exist() -> None:
    suite = load_suite()
    assert "default" in suite.harnesses
    spec = suite.harnesses["default"]
    assert (SUITE_PATH.parent.parent / spec.spec).is_file()


def test_steerable_is_the_harness_aware_agent() -> None:
    suite = load_suite()
    assert suite.agents["steerable"].accepts_harness is True
    # Baselines run as shipped: varying their harness is not our variable.
    for name in ("oracle", "claude-code", "codex", "pi", "pi-glm"):
        assert suite.agents[name].accepts_harness is False


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
    assert len(FAILED_PREV) == 31
    assert set(FAILED_PREV) <= suite.catalog_set
    assert FAILED_PREV == tuple(task for task in suite.catalog if task in set(FAILED_PREV))


def test_iteration_splits_are_catalog_subsets_that_do_not_overlap() -> None:
    """The two iteration sets ask different questions of different tasks.

    ``flaky`` is paired arms over tasks whose baseline is a coin toss;
    ``spiral-red`` is one arm over tasks with no passes to compare against.
    A task in both would be measured twice under designs that disagree about
    what its baseline is.
    """
    suite = load_suite()
    flaky = set(suite.splits["flaky"])
    spiral = set(suite.splits["spiral-red"])
    assert flaky <= suite.catalog_set
    assert spiral <= suite.catalog_set
    assert not flaky & spiral


def test_sharded_jobs_shard_over_their_whole_split() -> None:
    """A shard count below the split size silently drops the tail.

    ``--shards N`` and the matrix both have to move when a split grows, and
    nothing at runtime complains if they disagree: the ids past the last
    shard are simply never dispatched.
    """
    suite = load_suite()
    root = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    weekly = (root / "evals-weekly.yml").read_text()
    for split in ("flaky", "spiral-red"):
        size = len(suite.splits[split])
        assert f'--split {split} --shard "${{{{ matrix.shard }}}}" --shards {size}' in weekly
        assert f"shard: {list(range(size))}" in weekly


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


def test_pi_glm_runs_the_product_model_on_pis_harness() -> None:
    """The whole point of this agent is that only the harness differs from the
    steerable leg. A model or harbor drift makes the run unreadable."""
    suite = load_suite()
    pi_glm = suite.agents["pi-glm"]
    assert pi_glm.skipped is False
    assert pi_glm.harbor == PI_GLM_IMPORT_PATH
    assert pi_glm.model == "openrouter/z-ai/glm-5.3-flash"
    assert pi_glm.env_any == ("OPENROUTER_API_KEY",)
    assert pi_glm.accepts_harness is False
    assert suite.agents[PRODUCT_AGENT].model.split("/", 1)[1] == pi_glm.model.split("/", 1)[1]


def test_pi_glm_declares_the_wire_protocol_of_the_gateway() -> None:
    """Harbor's Pi agent raises unless `model_api` names the protocol spoken by
    OPENROUTER_BASE_URL, so a missing kwarg fails every trial at setup."""
    suite = load_suite()
    assert dict(suite.agents["pi-glm"].kwargs)["model_api"] == "openai-completions"


def test_pi_baseline_carries_no_gateway_kwargs() -> None:
    """`model_api` without a configured base URL is a hard error in Harbor's Pi
    agent, so the Claude leg must not inherit pi-glm's endpoint kwargs."""
    suite = load_suite()
    assert "model_api" not in dict(suite.agents["pi"].kwargs)


def test_both_pi_legs_pin_the_npm_version() -> None:
    """An unpinned install resolves `@latest`, so an upstream pi release moves
    the baseline between runs and no artifact can attribute the change."""
    suite = load_suite()
    for name in ("pi", "pi-glm"):
        pinned = dict(suite.agents[name].kwargs).get("version")
        assert pinned == "0.84.4", f"agents.{name} does not pin the pi version"


def test_pi_glm_argv_passes_the_gateway_protocol_to_harbor() -> None:
    suite = load_suite()
    argv = harbor_argv(
        suite,
        agent="pi-glm",
        tasks=("fix-git",),
        jobs_dir=Path("/tmp/jobs"),
    )
    assert "--agent" in argv and argv[argv.index("--agent") + 1] == PI_GLM_IMPORT_PATH
    assert argv[argv.index("--model") + 1] == "openrouter/z-ai/glm-5.3-flash"
    assert "--agent-kwarg" in argv
    kwargs = {argv[i + 1] for i, value in enumerate(argv) if value == "--agent-kwarg"}
    assert "model_api=openai-completions" in kwargs


def test_the_glm_legs_run_the_product_model_on_someone_elses_harness() -> None:
    """Each of these answers "same model, different harness", so a model or
    harbor drift makes the run unreadable the way pi-glm's first one was."""
    suite = load_suite()
    gateway_model = suite.agents[PRODUCT_AGENT].model.split("/", 1)[1]
    for name, base_url_env in (
        ("claude-code-glm", "ANTHROPIC_BASE_URL"),
        ("codex-glm", "OPENAI_BASE_URL"),
    ):
        spec = suite.agents[name]
        assert spec.skipped is False
        assert spec.harbor == GLM_LEG_IMPORT_PATHS[name]
        # No provider prefix: both adapters forward whatever survives their
        # own credential lookup, so the string has to be the gateway's id.
        assert spec.model == gateway_model
        # Readiness keys on the base URL rather than the key, because the
        # base URL is what distinguishes the leg from its own baseline.
        assert spec.env_any == (base_url_env,)
        assert spec.accepts_harness is False


def test_the_glm_legs_pin_their_cli_version() -> None:
    """An unpinned install resolves `@latest`, so an upstream release moves
    the baseline between runs and no artifact can attribute the change."""
    suite = load_suite()
    for name in ("claude-code-glm", "codex-glm"):
        pinned = dict(suite.agents[name].kwargs).get("version")
        assert pinned, f"agents.{name} does not pin its CLI version"


def test_claude_code_glm_sends_the_same_effort_as_the_product_leg() -> None:
    """Claude Code's `--effort` accepts `max`, which is what
    STEERABLE_REASONING_EFFORT sends, so effort is not one of the
    differences this leg measures. Unset, the CLI picks its own default."""
    suite = load_suite()
    assert dict(suite.agents["claude-code-glm"].kwargs)["reasoning_effort"] == "max"


def test_the_baseline_legs_carry_no_gateway_kwargs() -> None:
    """claude-code and codex exist to score their vendors' own models; a
    kwarg leaking from a `-glm` leg would change what they measure."""
    suite = load_suite()
    for name in ("claude-code", "codex"):
        assert "reasoning_effort" not in dict(suite.agents[name].kwargs)


def test_glm_leg_argv_passes_the_full_gateway_slug() -> None:
    """Harbor's Codex adapter truncates on the last `/`, so the argv has to
    carry the vendor segment for `restore_model_slug` to put back."""
    suite = load_suite()
    for name in ("claude-code-glm", "codex-glm"):
        argv = harbor_argv(suite, agent=name, tasks=("fix-git",), jobs_dir=Path("/tmp/jobs"))
        assert argv[argv.index("--agent") + 1] == GLM_LEG_IMPORT_PATHS[name]
        assert argv[argv.index("--model") + 1] == "z-ai/glm-5.3-flash"


def test_live_agents_include_product() -> None:
    suite = load_suite()
    assert LIVE_AGENTS == (
        "claude-code",
        "codex",
        "pi",
        "pi-glm",
        "claude-code-glm",
        "codex-glm",
        PRODUCT_AGENT,
    )
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
    # A sharded job that forgets the per-trial path uploads only its status
    # file, so its tasks read as absent instead of failed and the mean is
    # computed over a smaller set without saying so. Counted without the agent
    # directory because the catalog job takes its agent from the dispatch.
    assert weekly.count("*/*/result.json") == weekly.count(
        '--shard "${{ matrix.shard }}"'
    )
    assert "pull_request:" not in weekly
    assert "--split catalog" in weekly
    assert "--split failed-prev" in weekly
    assert "--shards 16" not in weekly
    assert weekly.count("--shards 24") == 1
    assert "--shards 32" not in weekly
    assert "--shards 36" not in weekly
    assert "--shards 48" not in weekly
    # The catalog shard count comes from the plan job: 49 for a full run, one
    # shard per task for a `tasks` rerun. A hardcoded count here would either
    # break the rerun or silently shrink the full run.
    assert "--shards 49" not in weekly
    assert '--shards "$SHARDS"' in weekly
    assert "count=49" in weekly
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
    # Catalog shards are planned dynamically so a `tasks` rerun does not
    # launch 41 empty jobs that fail on "selected no tasks".
    assert "shard: ${{ fromJSON(needs.catalog-plan.outputs.shards) }}" in weekly
    assert "timeout-minutes: 360" in weekly
    # The sharded splits run the long catalog tasks and need the generous
    # multipliers; cheap-12 stays at ×3 so a smoke run stays a smoke run.
    sharded = weekly.count('--shard "${{ matrix.shard }}"')
    assert weekly.count("--agent-timeout-multiplier 12") == sharded
    assert weekly.count("--verifier-timeout-multiplier 2") == sharded
    cheap_job = weekly.split("  failed-prev:", 1)[0]
    assert "--agent-timeout-multiplier 3" in cheap_job
    assert "--agent-timeout-multiplier 12" not in cheap_job
    assert "--verifier-timeout-multiplier" not in cheap_job
    assert "options:" in weekly
    assert "- failed-prev" in weekly
    assert "github.event.inputs.split == 'cheap-12'" in weekly
    assert "github.event.inputs.split != 'catalog'" not in weekly
    # The gateway key is only safe in OPENAI_API_KEY when OPENAI_BASE_URL
    # travels with it, which is exactly the codex-glm leg. Unpaired, Codex
    # sends our credential to api.openai.com.
    catalog_job = weekly.split("name: Harbor catalog shard", 1)[1]
    catalog_env = catalog_job.split("upload-artifact", 1)[0]
    assert catalog_env.count("OPENAI_API_KEY") == catalog_env.count("OPENAI_BASE_URL")
    failed_job = weekly.split("name: Harbor failed-prev shard", 1)[1]
    assert '--split failed-prev --shard "${{ matrix.shard }}" --shards 24' in weekly
    assert 'split_arg="--split catalog"' in weekly
    assert 'split_arg="--tasks $(echo "$TASKS" | tr \',\' \' \')"' in weekly
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
    assert sum(len(shard) for shard in failed) == 31
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
