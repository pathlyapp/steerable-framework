"""Multi-file atomic patch (W1.4.1.3).

Five files changed today means five ``edit_file`` round trips — five LLM
requests. ``apply_patch`` takes the whole change as one call: per file a
list of ``{oldText, newText}`` edits, located with the same three-tier
matching as ``edit_file`` (exact → whitespace-tolerant → unicode-normalized,
`file_edit.apply_edits`). No `*** Begin Patch` dialect, no unified-diff
line numbers — LLM-generated line numbers are wrong often enough that
content-anchored matching is the reliable primitive.

Atomicity is the other half: every file's edits are planned against the
original bytes first; any locate failure aborts the whole patch with
nothing written. A failure during the write phase (disk, permissions)
restores the files already written, so the workspace never holds a
half-applied state that would mislead the model's next step.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .file_edit import EditOp, apply_edits
from .workspace_fs import LOCAL_FS, WorkspaceFs, WorkspaceFsError


@dataclass(frozen=True, slots=True)
class FilePatch:
    """Edits to one file, repo-relative path."""

    path: str
    edits: tuple[EditOp, ...]


@dataclass(frozen=True, slots=True)
class PatchSummary:
    files_changed: tuple[str, ...]
    diffs: tuple[str, ...]


async def apply_patch(
    root: Path,
    patches: list[FilePatch],
    *,
    resolve: "callable[[str], Path] | None" = None,
    fs: WorkspaceFs = LOCAL_FS,
) -> PatchSummary:
    """Plan all files, then write all files; roll back on write failure.

    ``resolve`` maps a repo-relative path to an absolute one — the caller's
    workspace-jurisdiction check (``_resolve_under``) so the patch tool
    cannot escape the workspace even if this module is reused elsewhere.
    ``fs`` is the file-content channel (3.4.3.1): disk by default, the ACP
    client bridge inside an editor.
    """
    if not patches:
        raise ValueError("patches is empty — at least one file entry is required")
    seen: set[str] = set()
    for patch in patches:
        if patch.path in seen:
            raise ValueError(
                f"duplicate patch for {patch.path} — merge its edits into one entry"
            )
        seen.add(patch.path)

    resolve = resolve or (lambda p: (root / p).resolve())

    # Phase 1: plan everything against original bytes; nothing written yet.
    planned: list[tuple[Path, str, str, str]] = []  # (target, original, new, diff)
    for patch in patches:
        target = resolve(patch.path)
        try:
            original = await fs.read_text(target)
        except (OSError, WorkspaceFsError) as exc:
            raise ValueError(f"{patch.path}: cannot read ({exc}); patch aborted") from exc
        result = apply_edits(
            original,
            [EditOp(old_text=e.old_text, new_text=e.new_text) for e in patch.edits],
            file_path=patch.path,
        )
        planned.append((target, original, result.content, result.diff))

    # Phase 2: write; a mid-phase failure restores what was already written.
    written: list[tuple[Path, str]] = []
    try:
        for target, original, new_content, _ in planned:
            await fs.write_text(target, new_content)
            written.append((target, original))
    except (OSError, WorkspaceFsError) as exc:
        for target, original in written:
            try:
                await fs.write_text(target, original)
            except (OSError, WorkspaceFsError):
                pass  # rollback is best-effort; the raise names the failure
        raise ValueError(
            f"write failed mid-patch ({exc}); restored {len(written)} file(s)"
        ) from exc

    return PatchSummary(
        files_changed=tuple(patch.path for patch in patches),
        diffs=tuple(diff for _, _, _, diff in planned),
    )
