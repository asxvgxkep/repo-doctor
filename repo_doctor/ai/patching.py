"""Validation, preview, application, and exact rollback for AI patches."""

from __future__ import annotations

import difflib
import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import PatchValidationError, RollbackError
from .models import PatchProposal
from .parser import parse_patch_response
from .paths import has_excluded_directory, is_secret_path, normalize_relative_path

MAX_TARGET_BYTES = 1_000_000
MAX_REPLACEMENT_CHARACTERS = 50_000
MAX_SIZE_CHANGE = 20_000
MIN_PATCH_CONFIDENCE = 0.85


@dataclass(frozen=True)
class PreparedPatch:
    """A validated replacement plus an exact byte snapshot for rollback."""

    root: Path
    path: Path
    relative_path: str
    proposal: PatchProposal
    original_bytes: bytes
    updated_bytes: bytes


def _validate_proposal_object(proposal: PatchProposal) -> PatchProposal:
    if not isinstance(proposal, PatchProposal):
        raise PatchValidationError("AI provider returned an unexpected patch object.")
    import json

    try:
        return parse_patch_response(
            json.dumps(
                {
                    "file": proposal.file,
                    "old_text": proposal.old_text,
                    "new_text": proposal.new_text,
                    "reason": proposal.reason,
                    "confidence": proposal.confidence,
                }
            )
        )
    except Exception as error:
        if isinstance(error, PatchValidationError):
            raise
        raise PatchValidationError(str(error)) from error


def prepare_patch(
    root: Path, proposal: PatchProposal, *, expected_sha256: str | None = None
) -> PreparedPatch:
    """Validate an untrusted proposal without modifying the target file."""
    proposal = _validate_proposal_object(proposal)
    try:
        relative = normalize_relative_path(proposal.file)
    except ValueError as error:
        raise PatchValidationError(f"Patch path is unsafe: {error}.") from error
    if has_excluded_directory(relative) or is_secret_path(relative):
        raise PatchValidationError("Patch targets an ignored or secret-like file.")
    if proposal.confidence < MIN_PATCH_CONFIDENCE:
        raise PatchValidationError(f"Patch confidence must be at least {MIN_PATCH_CONFIDENCE:.2f}.")
    if max(len(proposal.old_text), len(proposal.new_text)) > MAX_REPLACEMENT_CHARACTERS:
        raise PatchValidationError("Patch replacement is too large.")
    if abs(len(proposal.new_text) - len(proposal.old_text)) > MAX_SIZE_CHANGE:
        raise PatchValidationError("Patch changes too much content in one replacement.")

    root = root.resolve()
    target = root.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = target.resolve(strict=True)
    except OSError as error:
        raise PatchValidationError(f"Patch target does not exist: {relative}.") from error
    if not resolved.is_relative_to(root):
        raise PatchValidationError("Patch target resolves outside the repository.")
    if target.is_symlink() or not resolved.is_file():
        raise PatchValidationError("Patch target must be a regular, non-symlink text file.")
    try:
        original = resolved.read_bytes()
    except OSError as error:
        raise PatchValidationError(f"Could not read patch target: {relative}.") from error
    if len(original) > MAX_TARGET_BYTES or b"\x00" in original:
        raise PatchValidationError("Patch target is too large or is not a text file.")
    if expected_sha256 and hashlib.sha256(original).hexdigest() != expected_sha256:
        raise PatchValidationError("Target file changed after AI analysis; run the command again.")
    try:
        text = original.decode("utf-8")
    except UnicodeError as error:
        raise PatchValidationError("Patch target is not UTF-8 text.") from error
    occurrences = text.count(proposal.old_text)
    if occurrences != 1:
        detail = "was not found" if occurrences == 0 else "is ambiguous"
        raise PatchValidationError(f"Patch old_text {detail}; expected exactly one occurrence.")
    updated = text.replace(proposal.old_text, proposal.new_text, 1).encode("utf-8")
    return PreparedPatch(root, resolved, relative, proposal, original, updated)


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace one file atomically while preserving its permission bits."""
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".repo-doctor-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_patch(prepared: PreparedPatch) -> None:
    """Apply an already validated replacement."""
    try:
        current = prepared.path.read_bytes()
        if current != prepared.original_bytes:
            raise PatchValidationError("Target file changed before patch application.")
        _atomic_write(prepared.path, prepared.updated_bytes)
    except PatchValidationError:
        raise
    except OSError as error:
        raise PatchValidationError(f"Could not apply patch to {prepared.relative_path}.") from error


def rollback_patch(prepared: PreparedPatch) -> None:
    """Restore the captured bytes and verify exact equality."""
    try:
        _atomic_write(prepared.path, prepared.original_bytes)
        if prepared.path.read_bytes() != prepared.original_bytes:
            raise OSError("restored bytes differ from snapshot")
    except OSError as error:
        raise RollbackError(
            f"Rollback failed for {prepared.relative_path}; restore it from Git before continuing."
        ) from error


def render_patch_diff(prepared: PreparedPatch) -> str:
    """Render a portable unified preview without executing external commands."""
    before = prepared.original_bytes.decode("utf-8").splitlines(keepends=True)
    after = prepared.updated_bytes.decode("utf-8").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"a/{prepared.relative_path}",
            tofile=f"b/{prepared.relative_path}",
        )
    )
