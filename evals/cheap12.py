"""Deterministic cheap-12 draw from catalog-89.

The 12-id smoke is a stratified sample of Terminal-Bench 2.1 catalog-89,
not a hand-picked easy subset. Three difficulty strata come from the six
``8e260de`` catalog runs (stable green 6/6, flaky mixed, stable red 0/6).
Hamilton allocation of n=12 is 7 / 4 / 1.

Within the flaky stratum, one id is taken from each of the 1/6, 2/6, 4/6,
and 5/6 pass-rate bands (the 3/6 band has a single id and loses the remainder
race). Sampling two 5/6 ids would be size-proportional inside flaky but
would shrink the variance of the 12-task mean — the opposite of why those
four slots exist.

Within a cell: shortest ``catalog_minutes``, skip exclusive-pack tasks and
vision-only ids (DeepSeek-V4-Flash is text-only). ``fix-git`` is forced
into the green seven so oracle-canary stays a subset.
"""

from __future__ import annotations

from evals.suite import EXCLUSIVE_PACK_TASKS

#: 0/6 on the six ``8e260de`` runs. Keep in lockstep with ``loss-34``.
STABLE_RED: frozenset[str] = frozenset(
    {
        "extract-moves-from-video",
        "filter-js-from-html",
        "gcode-to-text",
        "make-doom-for-mips",
        "protein-assembly",
        "regex-chess",
        "video-processing",
    }
)

#: Pass-rate bands inside ``splits.flaky`` (x/6). Union must equal that split.
FLAKY_BANDS: dict[str, tuple[str, ...]] = {
    "1/6": (
        "make-mips-interpreter",
        "raman-fitting",
        "winning-avg-corewars",
    ),
    "2/6": (
        "dna-assembly",
        "dna-insert",
        "model-extraction-relu-logits",
        "pytorch-model-cli",
    ),
    "3/6": ("path-tracing-reverse",),
    "4/6": (
        "bn-fit-modify",
        "build-pov-ray",
        "circuit-fibsqrt",
        "install-windows-3.11",
        "largest-eigenval",
        "mteb-retrieve",
        "schemelike-metacircular-eval",
        "train-fasttext",
    ),
    "5/6": (
        "caffe-cifar-10",
        "cobol-modernization",
        "db-wal-recovery",
        "extract-elf",
        "feal-linear-cryptanalysis",
        "git-multibranch",
        "gpt2-codegolf",
        "qemu-alpine-ssh",
        "sam-cell-seg",
        "sanitize-git-repo",
        "sparql-university",
    ),
}

#: One slot per band for the four flaky seats (3/6 loses Hamilton).
FLAKY_BAND_SLOTS: tuple[str, ...] = ("1/6", "2/6", "4/6", "5/6")

ORACLE_CANARY = "fix-git"

#: DeepSeek-V4-Flash is text-only; a vision task in cheap-12 would be a
#: structural zero on that model, not a harness reading.
_SKIP_VISION = frozenset({"code-from-image"})

N_GREEN = 7
N_FLAKY = 4
N_RED = 1


def flaky_ids() -> frozenset[str]:
    """Union of the pass-rate bands."""
    return frozenset(task for band in FLAKY_BANDS.values() for task in band)


def _shortest(
    candidates: list[str],
    minutes: dict[str, int],
    *,
    k: int,
    skip: frozenset[str],
) -> list[str]:
    eligible = [task for task in candidates if task not in skip]
    eligible.sort(key=lambda task: (minutes[task], task))
    if len(eligible) < k:
        raise ValueError(
            f"need {k} ids, have {len(eligible)} after skipping {sorted(skip)}"
        )
    return eligible[:k]


def select_cheap12(
    catalog: tuple[str, ...],
    minutes: dict[str, int],
) -> tuple[str, ...]:
    """Return the 12-id stratified sample, greens first then flaky then red."""
    catalog_set = set(catalog)
    flaky = flaky_ids()
    if not flaky <= catalog_set:
        raise ValueError("flaky band id missing from catalog")
    if not STABLE_RED <= catalog_set:
        raise ValueError("stable-red id missing from catalog")
    skip = EXCLUSIVE_PACK_TASKS | _SKIP_VISION
    green = [
        task
        for task in catalog
        if task not in flaky and task not in STABLE_RED
    ]
    if ORACLE_CANARY not in green:
        raise ValueError(f"{ORACLE_CANARY} is not a stable-green catalog id")
    green_rest = [task for task in green if task != ORACLE_CANARY]
    greens = [ORACLE_CANARY] + _shortest(
        green_rest, minutes, k=N_GREEN - 1, skip=skip
    )
    flaky_picks: list[str] = []
    for band in FLAKY_BAND_SLOTS:
        picked = _shortest(list(FLAKY_BANDS[band]), minutes, k=1, skip=skip)
        flaky_picks.extend(picked)
    reds = _shortest(sorted(STABLE_RED), minutes, k=N_RED, skip=skip)
    chosen = tuple(greens + flaky_picks + reds)
    if len(chosen) != 12 or len(set(chosen)) != 12:
        raise ValueError(f"cheap-12 draw is not 12 unique ids: {chosen}")
    return chosen
