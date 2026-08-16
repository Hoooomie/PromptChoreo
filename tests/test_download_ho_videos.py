import importlib.util
import struct
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "download_ho_videos.py"
SPEC = importlib.util.spec_from_file_location("download_ho_videos", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

collection_from_payload = MODULE.collection_from_payload
exploration_from_item = MODULE.exploration_from_item
normalize_date = MODULE.normalize_date
records_for_explorations = MODULE.records_for_explorations
sanitize_filename = MODULE.sanitize_filename
validate_media_file = MODULE.validate_media_file


def test_collection_unwraps_exploration_records_response():
    payload = {
        "code": 0,
        "data": {
            "records": [{"id": "one"}, {"id": "two"}],
            "totalCount": 25,
            "page": 1,
        },
    }

    items, total = collection_from_payload(payload)

    assert items == [{"id": "one"}, {"id": "two"}]
    assert total == 25


def test_plain_list_has_unknown_total_for_pagination():
    items, total = collection_from_payload([{"id": 1}])

    assert items == [{"id": 1}]
    assert total is None


def test_exploration_prefers_composed_video_over_preview():
    item = {
        "explorationId": "5wjKKg-aAfiliByhk3o4AR5dkWJWTzz0eu8e0LFZZ4E",
        "title": "Art of the Polish",
        "createdAt": "2026-07-26T22:17:00+08:00",
        "previewVideoUrl": "https://cdn.example/preview/demo.mp4?v=1",
        "download": {
            "composeVideoUrl": (
                "https://cdn.example/uploads/media-process/compose_branch_video/"
                "2026-07-26/final.mp4?v=2"
            )
        },
    }

    exploration = exploration_from_item(item)

    assert exploration.exploration_id.startswith("5wjKKg-")
    assert exploration.date == "2026.07.26"
    assert exploration.video_urls == (
        "https://cdn.example/uploads/media-process/compose_branch_video/"
        "2026-07-26/final.mp4?v=2",
    )


def test_multiple_branch_videos_get_stable_distinct_names():
    item = {
        "id": "explore-1234567890",
        "name": "Same title",
        "create_time": "2026.08.05",
        "videoList": [
            {
                "preview": "https://cdn.example/one-preview.mp4",
                "original": "https://cdn.example/one.mp4?token=old",
            },
            {"original": "https://cdn.example/two.mp4"},
        ],
    }
    exploration = exploration_from_item(item)

    records = records_for_explorations([exploration], site="cn")

    assert len(records) == 2
    assert records[0].key != records[1].key
    assert records[0].filename == "2026.08.05_Same title_eexplore-1234_v01.mp4"
    assert records[1].filename.endswith("_v02.mp4")


def test_missing_media_url_still_supports_official_download():
    exploration = exploration_from_item(
        {"id": "detail-only", "title": "Detail only", "createdAt": "2026-08-05"}
    )

    records = records_for_explorations([exploration], site="cn")

    assert len(records) == 1
    assert records[0].url is None
    assert records[0].stable_media_id == "official"
    assert records[0].detail_path == "/profile/exploration/detail-only"


def test_date_and_filename_normalization():
    assert normalize_date("2026-8-5 12:00") == "2026.08.05"
    assert normalize_date("not a date") == ""
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename('bad<name>:test ') == "bad_name__test"


def _box(box_type: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", len(payload) + 8, box_type) + payload


def test_media_validation_accepts_structurally_valid_mp4(tmp_path: Path):
    video = tmp_path / "video.part"
    video.write_bytes(
        _box(b"ftyp", b"isom\x00\x00\x02\x00isom")
        + _box(b"moov", b"\x00" * 1024)
        + _box(b"mdat", b"\x00" * 32)
    )

    assert validate_media_file(video) == "mp4"


def test_media_validation_rejects_html_error_page(tmp_path: Path):
    video = tmp_path / "video.part"
    video.write_bytes(b"<html>login required</html>" * 100)

    with pytest.raises(ValueError, match="不是 MP4/WebM"):
        validate_media_file(video)
