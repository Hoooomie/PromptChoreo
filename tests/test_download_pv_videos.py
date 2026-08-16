import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "download_pv_videos.py"
SPEC = importlib.util.spec_from_file_location("download_pv_videos", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

collection_from_payload = MODULE.collection_from_payload
normalize_date = MODULE.normalize_date
reconcile_download_state = MODULE.reconcile_download_state
sanitize_filename = MODULE.sanitize_filename
video_records_for_preset = MODULE.video_records_for_preset


def test_collection_unwraps_pixverse_response():
    payload = {
        "code": 0,
        "data": {
            "items": [{"id": 1}, {"id": 2}],
            "total": 9,
        },
    }

    items, total = collection_from_payload(payload)

    assert items == [{"id": 1}, {"id": 2}]
    assert total == 9


def test_primary_video_falls_back_to_example_session():
    preset = {
        "id": 42,
        "title": "Demo world",
        "created_at": "2026-07-19T09:30:00Z",
        "example_sessions": [
            {
                "session_id": 7,
                "final_video_url": "https://world-media.pixverse.ai/a/demo.mp4?token=one",
            }
        ],
    }

    records = video_records_for_preset(preset)

    assert len(records) == 1
    assert records[0].date == "2026.07.19"
    assert records[0].session_id is None
    assert records[0].filename == "2026.07.19_Demo world_p42.mp4"


def test_all_sessions_deduplicates_rotating_signed_urls():
    preset = {
        "id": "99",
        "title": "World",
        "created_at": "2026.08.04",
        "video_uri": "https://media.example/video/main.mp4?signature=old",
        "example_sessions": [
            {
                "session_id": "a",
                "final_video_url": "https://media.example/video/main.mp4?signature=new",
            }
        ],
    }
    sessions = [
        {
            "session_id": "b",
            "final_video_url": "https://media.example/video/second.webm",
        }
    ]

    records = video_records_for_preset(
        preset,
        sessions,
        include_all_sessions=True,
    )

    assert [record.session_id for record in records] == [None, "b"]
    assert records[1].filename == "2026.08.04_World_p99_sb.webm"


def test_source_session_supplies_new_world_primary_video():
    preset = {
        "id": 275441,
        "title": "New world",
        "created_at": "2026-08-05T08:40:02.121000Z",
        "video_uri": None,
        "source_session_id": 167632910766720,
    }
    source_session = {
        "session_id": 167632910766720,
        "final_video_url": "https://world-media.pixverse.ai/output/result.mp4",
    }

    records = video_records_for_preset(preset, source_session=source_session)

    assert len(records) == 1
    assert records[0].url == source_session["final_video_url"]
    assert records[0].session_id is None
    assert records[0].filename == "2026.08.05_New world_p275441.mp4"


def test_date_and_filename_normalization():
    assert normalize_date("2026-8-4 13:00:00") == "2026.08.04"
    assert normalize_date("no date") == ""
    assert sanitize_filename('CON') == "_CON"
    assert sanitize_filename('bad<name>:test ') == "bad_name__test"


def test_stale_download_record_is_removed_when_file_was_deleted(tmp_path):
    record = video_records_for_preset(
        {
            "id": 42,
            "title": "Deleted video",
            "created_at": "2026-08-05",
            "video_uri": "https://media.example/video.mp4",
        }
    )[0]
    downloaded = {record.key}
    destination = tmp_path / record.filename

    should_skip, removed_stale_record = reconcile_download_state(
        record,
        downloaded,
        destination,
    )

    assert should_skip is False
    assert removed_stale_record is True
    assert record.key not in downloaded


def test_download_record_still_skips_when_file_exists(tmp_path):
    record = video_records_for_preset(
        {
            "id": 42,
            "title": "Existing video",
            "created_at": "2026-08-05",
            "video_uri": "https://media.example/video.mp4",
        }
    )[0]
    downloaded = {record.key}
    destination = tmp_path / record.filename
    destination.write_bytes(b"video")

    should_skip, removed_stale_record = reconcile_download_state(
        record,
        downloaded,
        destination,
    )

    assert should_skip is True
    assert removed_stale_record is False
    assert record.key in downloaded
