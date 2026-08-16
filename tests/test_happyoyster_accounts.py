import json

import pytest

from scripts.happyoyster_accounts import (
    AccountPool,
    AccountPoolExhausted,
    STATE_VERSION,
    VIDEOS_PER_ACCOUNT,
    load_accounts,
)


def test_load_accounts_accepts_wrapped_and_chinese_keys(tmp_path):
    source = tmp_path / "accounts.json"
    source.write_text(
        json.dumps(
            {
                "accounts": [
                    {"邮箱": "first@example.com", "密码": "secret-1"},
                    {"email": "second@example.com", "password": "secret-2"},
                ]
            }
        ),
        encoding="utf-8",
    )

    accounts = load_accounts(source)

    assert [account.email for account in accounts] == [
        "first@example.com",
        "second@example.com",
    ]
    assert [account.password for account in accounts] == [
        "secret-1",
        "secret-2",
    ]
    assert "first@example.com" not in repr(accounts[0])
    assert "secret-1" not in repr(accounts[0])


def test_account_pool_allows_one_successful_video_per_account(tmp_path):
    source = tmp_path / "accounts.json"
    state = tmp_path / "usage.json"
    source.write_text(
        json.dumps(
            [
                {"email": "first@example.com", "password": "secret-1"},
                {"email": "second@example.com", "password": "secret-2"},
            ]
        ),
        encoding="utf-8",
    )
    pool = AccountPool(source, state)

    first = pool.claim_next("job-1")
    pool.mark_finished(first, "job-1", success=True)
    second = pool.claim_next("job-2")
    pool.mark_finished(second, "job-2", success=False)

    assert first.email == "first@example.com"
    assert second.email == "second@example.com"
    assert pool.available_count == 1
    assert pool.remaining_video_slots == 1
    retry = pool.claim_next("job-2-retry")
    assert retry.email == "second@example.com"
    pool.mark_finished(retry, "job-2-retry", success=True)
    assert pool.available_count == 0
    assert pool.remaining_video_slots == 0
    with pytest.raises(AccountPoolExhausted):
        pool.claim_next("job-3")

    state_text = state.read_text(encoding="utf-8")
    assert "first@example.com" not in state_text
    assert "second@example.com" not in state_text
    assert "secret-1" not in state_text
    assert "secret-2" not in state_text


def test_version_one_completed_account_remains_exhausted(tmp_path):
    source = tmp_path / "accounts.json"
    state = tmp_path / "usage.json"
    source.write_text(
        json.dumps(
            [{"email": "legacy@example.com", "password": "secret"}]
        ),
        encoding="utf-8",
    )
    account = load_accounts(source)[0]
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": {
                    account.fingerprint: {
                        "status": "completed",
                        "job_id": "old-job",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    pool = AccountPool(source, state)
    assert VIDEOS_PER_ACCOUNT == 1
    assert pool.available_count == 0
    assert pool.remaining_video_slots == 0
    with pytest.raises(AccountPoolExhausted):
        pool.claim_next("new-job")


def test_new_account_is_selected_before_retrying_failed_account(tmp_path):
    source = tmp_path / "accounts.json"
    state = tmp_path / "usage.json"
    source.write_text(
        json.dumps(
            [
                {"email": "failed@example.com", "password": "one"},
                {"email": "new@example.com", "password": "two"},
            ]
        ),
        encoding="utf-8",
    )
    pool = AccountPool(source, state)

    failed = pool.claim_next("failed-job")
    pool.mark_finished(failed, "failed-job", success=False)
    selected = pool.claim_next("next-job")

    assert failed.email == "failed@example.com"
    assert selected.email == "new@example.com"


def test_duplicate_email_is_rejected_case_insensitively(tmp_path):
    source = tmp_path / "accounts.json"
    source.write_text(
        json.dumps(
            [
                {"email": "Same@example.com", "password": "one"},
                {"email": "same@example.com", "password": "two"},
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="邮箱重复"):
        load_accounts(source)


def test_state_file_cannot_overwrite_account_source(tmp_path):
    source = tmp_path / "accounts.json"
    source.write_text(
        json.dumps([{"email": "one@example.com", "password": "secret"}]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="不能与账号源 JSON"):
        AccountPool(source, source)
