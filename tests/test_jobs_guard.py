"""자동 발송 '하루 한 번 시도' 규칙 테스트."""
import pytest

from src import db, jobs

CID, RM, DAY = "skmns", "2026-10", "2026-11-20"


@pytest.fixture()
def tmpdb(tmp_path):
    db.set_db_path(tmp_path / "t.db")
    db.init_db()
    yield
    db.set_db_path(None)


def _log(kind, mode, status):
    db.log_mail(CID, RM, kind, mode, ["a@example.com"], "s", status)


def test_no_attempt_means_not_attempted(tmpdb):
    assert not jobs._auto_attempted_today(CID, RM, DAY, "live")


def test_same_mode_attempt_blocks_retry_same_day(tmpdb):
    _log("customer", "live", "sent")
    assert jobs._auto_attempted_today(CID, RM, DAY, "live")


def test_failed_attempt_also_counts(tmpdb):
    _log("customer", "live", "failed")
    assert jobs._auto_attempted_today(CID, RM, DAY, "live")


def test_dryrun_attempt_does_not_block_live(tmpdb):
    _log("customer", "dryrun", "saved")
    assert jobs._auto_attempted_today(CID, RM, DAY, "dryrun")
    assert not jobs._auto_attempted_today(CID, RM, DAY, "live")


def test_attempts_before_reset_are_ignored(tmpdb):
    _log("customer", "live", "sent")
    _log("reset", "-", "reset")
    assert not jobs._auto_attempted_today(CID, RM, DAY, "live")
    _log("customer", "live", "sent")
    assert jobs._auto_attempted_today(CID, RM, DAY, "live")


def test_other_day_not_counted(tmpdb):
    _log("customer", "live", "sent")
    assert not jobs._auto_attempted_today(CID, RM, "2026-11-21", "live")
