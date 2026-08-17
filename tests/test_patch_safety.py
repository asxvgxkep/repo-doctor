import hashlib
from pathlib import Path

import pytest

from repo_doctor.ai.errors import PatchValidationError
from repo_doctor.ai.models import PatchProposal
from repo_doctor.ai.patching import apply_patch, prepare_patch, rollback_patch


def proposal(file: str = "app.py", old_text: str = "before", new_text: str = "after"):
    return PatchProposal(file, old_text, new_text, "Correct the behavior", 0.95)


def test_safe_patch_can_be_applied_and_rolled_back_exactly(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    original = b"value = 'before'\r\n"
    target.write_bytes(original)
    prepared = prepare_patch(tmp_path, proposal(old_text="'before'", new_text="'after'"))
    apply_patch(prepared)
    assert target.read_bytes() == b"value = 'after'\r\n"
    rollback_patch(prepared)
    assert target.read_bytes() == original


def test_ambiguous_old_text_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("before before\n", encoding="utf-8")
    with pytest.raises(PatchValidationError, match="ambiguous"):
        prepare_patch(tmp_path, proposal())


def test_missing_old_text_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("something else\n", encoding="utf-8")
    with pytest.raises(PatchValidationError, match="not found"):
        prepare_patch(tmp_path, proposal())


def test_secret_target_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("before", encoding="utf-8")
    with pytest.raises(PatchValidationError, match="secret"):
        prepare_patch(tmp_path, proposal(file=".env"))


def test_file_changed_after_analysis_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("before", encoding="utf-8")
    analyzed_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    target.write_text("new before", encoding="utf-8")
    with pytest.raises(PatchValidationError, match="changed after AI analysis"):
        prepare_patch(tmp_path, proposal(), expected_sha256=analyzed_hash)


def test_low_confidence_patch_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("before", encoding="utf-8")
    unsafe = PatchProposal("app.py", "before", "after", "Maybe", 0.5)
    with pytest.raises(PatchValidationError, match="confidence"):
        prepare_patch(tmp_path, unsafe)
