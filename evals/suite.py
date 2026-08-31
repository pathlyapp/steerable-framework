"""Load and validate `evals/suite.yaml`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import yaml

SUITE_PATH = Path(__file__).resolve().parent / "suite.yaml"
BASELINE_AGENTS = ("claude-code", "codex", "pi")
PRODUCT_AGENT = "steerable"
LIVE_AGENTS = (*BASELINE_AGENTS, PRODUCT_AGENT)
REQUIRED_AGENTS = ("oracle", "claude-code", "codex", "pi", "dsh", PRODUCT_AGENT)
STEERABLE_IMPORT_PATH = "evals.harbor_steerable:SteerableHarborAgent"
PINNED_HARBOR_VERSION = "0.22.0"
_SHA1_HEX_LEN = 40
# QEMU/VNC, MIPS ELF compiles, long ffmpeg/OCR, and wall-clock SQL
# speed tests need the whole 4-vCPU runner. n-concurrent=2 otherwise
# shares the box; keyboard screenshots stall, qemu boot stalls, the
# ELF never appears, extract-moves OCR takes minutes per frame, and
# query-optimize's median SQL time misses the 1.05× golden bound.
EXCLUSIVE_PACK_TASKS = frozenset(
    {
        "install-windows-3.11",
        "qemu-startup",
        "qemu-alpine-ssh",
        "make-doom-for-mips",
        "make-mips-interpreter",
        "extract-moves-from-video",
        "query-optimize",
    }
)


@dataclass(frozen=True)
class AgentSpec:
    name: str
    harbor: str | None
    model: str | None
    env_any: tuple[str, ...]
    kwargs: tuple[tuple[str, str], ...]
    skipped: bool
    reason: str | None


@dataclass(frozen=True)
class Suite:
    dataset_name: str
    git: str
    git_rev: str
    catalog: tuple[str, ...]
    splits: dict[str, tuple[str, ...]]
    n_attempts: int
    n_concurrent: int
    jobs_dir: str
    harbor_version: str
    catalog_minutes: dict[str, int]
    pack_floor_minutes: int
    agents: dict[str, AgentSpec]

    @property
    def catalog_set(self) -> frozenset[str]:
        return frozenset(self.catalog)


class SuiteError(ValueError):
    """Invalid suite YAML or an illegal task/agent selection."""


def load_suite(path: Path | None = None) -> Suite:
    source = path or SUITE_PATH
    raw = yaml.safe_load(source.read_text())
    if not isinstance(raw, dict):
        raise SuiteError(f"{source} must be a mapping")
    return _parse_suite(raw, source)


def agent_ready(spec: AgentSpec, environ: Mapping[str, str] | None = None) -> bool:
    """Return whether `spec` can be invoked given `environ`."""
    if spec.skipped:
        return False
    if not spec.env_any:
        return True
    env = environ if environ is not None else os.environ
    return any((env.get(name) or "").strip() for name in spec.env_any)


def missing_env(spec: AgentSpec, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    if spec.skipped or not spec.env_any:
        return ()
    env = environ if environ is not None else os.environ
    if any((env.get(name) or "").strip() for name in spec.env_any):
        return ()
    return spec.env_any


def resolve_tasks(
    suite: Suite,
    split: str,
    tasks: Sequence[str] | None = None,
) -> tuple[str, ...]:
    if tasks:
        unknown = [task for task in tasks if task not in suite.catalog_set]
        if unknown:
            raise SuiteError(f"tasks not in catalog: {', '.join(unknown)}")
        return tuple(tasks)
    selected = suite.splits.get(split)
    if selected is None:
        known = ", ".join(sorted(suite.splits))
        raise SuiteError(f"unknown split {split!r}; expected one of {known}")
    return selected


def shard_tasks(
    tasks: Sequence[str],
    *,
    shard: int,
    shards: int,
    minutes: Mapping[str, int] | None = None,
    pack_floor: int | None = None,
) -> tuple[str, ...]:
    """Split ``tasks`` into ``shards`` slices for GHA catalog jobs.

    With ``minutes``, pack longest-first onto the current lightest shard so
    wall-clock stays even. ``pack_floor`` raises each weight so a 170-minute
    wrap cannot stack six "short" recorded tasks onto one 360-minute job.
    Without minutes, round-robin by catalog order.
    """
    if shards < 1:
        raise SuiteError("shards must be >= 1")
    if shard < 0 or shard >= shards:
        raise SuiteError(f"shard {shard} out of range 0..{shards - 1}")
    if minutes:
        return _pack_by_minutes(tasks, shards, minutes, pack_floor=pack_floor)[
            shard
        ]
    return tuple(task for index, task in enumerate(tasks) if index % shards == shard)


def _pack_by_minutes(
    tasks: Sequence[str],
    shards: int,
    minutes: Mapping[str, int],
    *,
    pack_floor: int | None = None,
) -> tuple[tuple[str, ...], ...]:
    known = [int(minutes[task]) for task in tasks if task in minutes]
    default = sorted(known)[len(known) // 2] if known else 15
    floor = pack_floor if pack_floor and pack_floor > 0 else 0

    def weight(task: str) -> int:
        return max(int(minutes.get(task, default)), floor)

    loads = [0] * shards
    bins: list[list[str]] = [[] for _ in range(shards)]
    exclusive_ids = [task for task in tasks if task in EXCLUSIVE_PACK_TASKS]
    rest_ids = [task for task in tasks if task not in EXCLUSIVE_PACK_TASKS]
    remaining_shards = shards - len(exclusive_ids)
    isolate = bool(
        floor
        and exclusive_ids
        and remaining_shards >= 1
        and (len(rest_ids) + remaining_shards - 1) // remaining_shards <= 4
    )
    exclusive = exclusive_ids if isolate else []
    rest = rest_ids if isolate else list(tasks)
    exclusive.sort(key=lambda task: (-weight(task), task))
    rest.sort(key=lambda task: (-weight(task), task))

    def lightest(candidates: Sequence[int]) -> int:
        return min(candidates, key=lambda item: (loads[item], len(bins[item]), item))

    exclusive_bins: set[int] = set()
    for task in exclusive:
        empty = [i for i in range(shards) if not bins[i]]
        index = lightest(empty) if empty else lightest(range(shards))
        bins[index].append(task)
        loads[index] += weight(task)
        exclusive_bins.add(index)
    for task in rest:
        open_bins = [i for i in range(shards) if i not in exclusive_bins]
        index = lightest(open_bins if open_bins else range(shards))
        bins[index].append(task)
        loads[index] += weight(task)
    catalog_order = {task: position for position, task in enumerate(tasks)}
    for bucket in bins:
        bucket.sort(key=lambda task: catalog_order[task])
    return tuple(tuple(bucket) for bucket in bins)


def dataset_org(dataset_name: str) -> str:
    """Return the Harbor org prefix from a `org/dataset` package name."""
    if "/" not in dataset_name:
        raise SuiteError(f"dataset name {dataset_name!r} must be org/name")
    return dataset_name.split("/", 1)[0]


def harbor_task_name(dataset_name: str, task: str) -> str:
    """Harbor `--include-task-name` id (`org/short-id`).

    Suite YAML stores short ids (`fix-git`). Package datasets publish
    `terminal-bench/fix-git`; a bare short id matches nothing.
    """
    org = dataset_org(dataset_name)
    prefix = f"{org}/"
    if task.startswith(prefix):
        return task
    return f"{prefix}{task}"


def harbor_argv(
    suite: Suite,
    *,
    agent: str,
    tasks: Sequence[str],
    jobs_dir: Path,
    model: str | None = None,
    n_concurrent: int | None = None,
    n_attempts: int | None = None,
    agent_setup_timeout_multiplier: float | None = None,
    environment_build_timeout_multiplier: float | None = None,
    agent_timeout_multiplier: float | None = None,
    verifier_timeout_multiplier: float | None = None,
    harbor_bin: str = "harbor",
) -> list[str]:
    spec = suite.agents.get(agent)
    if spec is None:
        known = ", ".join(sorted(suite.agents))
        raise SuiteError(f"unknown agent {agent!r}; expected one of {known}")
    if spec.skipped or not spec.harbor:
        reason = spec.reason or "agent is skipped"
        raise SuiteError(f"agent {agent!r} cannot run Harbor: {reason}")
    if not tasks:
        raise SuiteError("task list is empty")

    argv = [
        harbor_bin,
        "run",
        "--dataset",
        suite.dataset_name,
        "--agent",
        spec.harbor,
    ]
    chosen_model = model if model is not None else spec.model
    if chosen_model:
        argv.extend(["--model", chosen_model])
    argv.extend(
        [
            "--yes",
            "--n-attempts",
            str(n_attempts if n_attempts is not None else suite.n_attempts),
            "--n-concurrent",
            str(n_concurrent if n_concurrent is not None else suite.n_concurrent),
            "--jobs-dir",
            str(jobs_dir),
        ]
    )
    if agent_setup_timeout_multiplier is not None:
        argv.extend(
            [
                "--agent-setup-timeout-multiplier",
                str(agent_setup_timeout_multiplier),
            ]
        )
    if environment_build_timeout_multiplier is not None:
        argv.extend(
            [
                "--environment-build-timeout-multiplier",
                str(environment_build_timeout_multiplier),
            ]
        )
    if agent_timeout_multiplier is not None:
        argv.extend(
            [
                "--agent-timeout-multiplier",
                str(agent_timeout_multiplier),
            ]
        )
    if verifier_timeout_multiplier is not None:
        argv.extend(
            [
                "--verifier-timeout-multiplier",
                str(verifier_timeout_multiplier),
            ]
        )
    for key, value in spec.kwargs:
        argv.extend(["--agent-kwarg", f"{key}={value}"])
    for task in tasks:
        argv.extend(["--include-task-name", harbor_task_name(suite.dataset_name, task)])
    return argv


def _parse_suite(raw: dict, source: Path) -> Suite:
    dataset = raw.get("dataset") or {}
    run = raw.get("run") or {}
    splits_raw = raw.get("splits") or {}
    agents_raw = raw.get("agents") or {}

    dataset_name = _require_str(dataset.get("name"), "dataset.name", source)
    git = _require_str(dataset.get("git"), "dataset.git", source)
    git_rev = _require_str(dataset.get("git_rev"), "dataset.git_rev", source)
    if len(git_rev) != _SHA1_HEX_LEN or any(c not in "0123456789abcdef" for c in git_rev):
        raise SuiteError(f"{source}: dataset.git_rev must be a 40-char lowercase SHA1")

    catalog = _id_tuple(splits_raw.get("catalog"), "splits.catalog", source)
    if len(catalog) != len(set(catalog)):
        raise SuiteError(f"{source}: splits.catalog contains duplicate ids")

    splits: dict[str, tuple[str, ...]] = {}
    catalog_set = frozenset(catalog)
    for name, value in splits_raw.items():
        ids = catalog if name == "catalog" else _id_tuple(value, f"splits.{name}", source)
        extra = [task for task in ids if task not in catalog_set]
        if extra:
            raise SuiteError(f"{source}: splits.{name} not in catalog: {', '.join(extra)}")
        splits[name] = ids

    if "cheap-12" not in splits:
        raise SuiteError(f"{source}: splits.cheap-12 is required")
    if len(splits["cheap-12"]) != 12:
        raise SuiteError(f"{source}: splits.cheap-12 must contain exactly 12 ids")
    if "oracle-canary" not in splits:
        raise SuiteError(f"{source}: splits.oracle-canary is required")

    agents = {}
    for name, body in agents_raw.items():
        if not isinstance(body, dict):
            raise SuiteError(f"{source}: agents.{name} must be a mapping")
        kwargs_raw = body.get("kwargs") or {}
        if not isinstance(kwargs_raw, dict):
            raise SuiteError(f"{source}: agents.{name}.kwargs must be a mapping")
        harbor = body.get("harbor")
        model = body.get("model")
        agents[name] = AgentSpec(
            name=name,
            harbor=None if harbor is None else _require_str(harbor, f"agents.{name}.harbor", source),
            model=None if model is None else _require_str(model, f"agents.{name}.model", source),
            env_any=tuple(body.get("env_any") or ()),
            kwargs=tuple((str(k), str(v)) for k, v in kwargs_raw.items()),
            skipped=bool(body.get("skipped")),
            reason=body.get("reason"),
        )

    missing_agents = [name for name in REQUIRED_AGENTS if name not in agents]
    if missing_agents:
        raise SuiteError(f"{source}: missing agents: {', '.join(missing_agents)}")
    dsh = agents["dsh"]
    if not dsh.skipped:
        raise SuiteError(f"{source}: agents.dsh must be skipped until a Harbor adapter exists")
    if agents["pi"].harbor != "pi":
        raise SuiteError(f"{source}: agents.pi.harbor must be 'pi' (Harbor first-party agent)")
    steerable = agents[PRODUCT_AGENT]
    if steerable.skipped:
        raise SuiteError(f"{source}: agents.{PRODUCT_AGENT} must not be skipped")
    if steerable.harbor != STEERABLE_IMPORT_PATH:
        raise SuiteError(
            f"{source}: agents.{PRODUCT_AGENT}.harbor must be {STEERABLE_IMPORT_PATH!r}"
        )
    harbor_version = _require_str(
        run.get("harbor_version"), "run.harbor_version", source
    )
    if harbor_version != PINNED_HARBOR_VERSION:
        raise SuiteError(
            f"{source}: run.harbor_version must be {PINNED_HARBOR_VERSION!r}"
        )
    catalog_minutes = _catalog_minutes(run.get("catalog_minutes"), catalog, source)
    pack_floor_minutes = _require_int(
        run.get("pack_floor_minutes"), "run.pack_floor_minutes", source
    )
    if pack_floor_minutes < 1:
        raise SuiteError(f"{source}: run.pack_floor_minutes must be >= 1")

    return Suite(
        dataset_name=dataset_name,
        git=git,
        git_rev=git_rev,
        catalog=catalog,
        splits=splits,
        n_attempts=_require_int(run.get("n_attempts"), "run.n_attempts", source),
        n_concurrent=_require_int(run.get("n_concurrent"), "run.n_concurrent", source),
        jobs_dir=_require_str(run.get("jobs_dir"), "run.jobs_dir", source),
        harbor_version=harbor_version,
        catalog_minutes=catalog_minutes,
        pack_floor_minutes=pack_floor_minutes,
        agents=agents,
    )


def _catalog_minutes(value: object, catalog: tuple[str, ...], source: Path) -> dict[str, int]:
    if not isinstance(value, dict) or not value:
        raise SuiteError(f"{source}: run.catalog_minutes must be a mapping of catalog ids")
    catalog_set = frozenset(catalog)
    minutes: dict[str, int] = {}
    for key, raw in value.items():
        if key not in catalog_set:
            raise SuiteError(f"{source}: run.catalog_minutes unknown id {key}")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise SuiteError(f"{source}: run.catalog_minutes.{key} must be a positive integer")
        minutes[str(key)] = raw
    missing = [task for task in catalog if task not in minutes]
    if missing:
        raise SuiteError(
            f"{source}: run.catalog_minutes missing {', '.join(missing)}"
        )
    return minutes


def _require_str(value: object, field: str, source: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SuiteError(f"{source}: {field} must be a non-empty string")
    return value


def _require_int(value: object, field: str, source: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SuiteError(f"{source}: {field} must be an integer")
    return value


def _id_tuple(value: object, field: str, source: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SuiteError(f"{source}: {field} must be a non-empty list")
    ids = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SuiteError(f"{source}: {field} entries must be non-empty strings")
        ids.append(item)
    return tuple(ids)
