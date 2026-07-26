"""Small media-file helpers used by benchmark runners."""

from __future__ import annotations

import os
import struct
from typing import BinaryIO, Iterator


def _iter_mp4_boxes(
    stream: BinaryIO, start: int, end: int
) -> Iterator[tuple[bytes, int, int]]:
    """Yield ``(box_type, payload_start, box_end)`` within an MP4 box range."""
    position = start
    while position + 8 <= end:
        stream.seek(position)
        header = stream.read(8)
        if len(header) != 8:
            return

        size, box_type = struct.unpack(">I4s", header)
        header_size = 8
        if size == 1:
            extended_size = stream.read(8)
            if len(extended_size) != 8:
                return
            size = struct.unpack(">Q", extended_size)[0]
            header_size = 16
        elif size == 0:
            size = end - position

        if size < header_size or position + size > end:
            return

        box_end = position + size
        yield box_type, position + header_size, box_end
        position = box_end


def get_mp4_duration_seconds(path: str | os.PathLike[str]) -> float | None:
    """Read the movie duration from an MP4 ``mvhd`` box.

    Returns ``None`` when the file is missing, malformed, or has no usable
    movie header. Large video payloads are skipped with seeks.
    """
    try:
        file_size = os.path.getsize(path)
        with open(path, "rb") as stream:
            moov = next(
                (
                    (payload_start, box_end)
                    for box_type, payload_start, box_end in _iter_mp4_boxes(
                        stream, 0, file_size
                    )
                    if box_type == b"moov"
                ),
                None,
            )
            if moov is None:
                return None

            for box_type, payload_start, _ in _iter_mp4_boxes(
                stream, moov[0], moov[1]
            ):
                if box_type != b"mvhd":
                    continue

                stream.seek(payload_start)
                version_and_flags = stream.read(4)
                if len(version_and_flags) != 4:
                    return None

                version = version_and_flags[0]
                if version == 0:
                    fields = stream.read(16)
                    if len(fields) != 16:
                        return None
                    _, _, timescale, duration = struct.unpack(">IIII", fields)
                    unknown_duration = 0xFFFFFFFF
                elif version == 1:
                    fields = stream.read(28)
                    if len(fields) != 28:
                        return None
                    _, _, timescale, duration = struct.unpack(">QQIQ", fields)
                    unknown_duration = 0xFFFFFFFFFFFFFFFF
                else:
                    return None

                if timescale == 0 or duration == unknown_duration:
                    return None
                return duration / timescale
    except (OSError, struct.error):
        return None

    return None


def _read_mdhd(stream: BinaryIO, payload_start: int) -> tuple[int, int] | None:
    stream.seek(payload_start)
    version_and_flags = stream.read(4)
    if len(version_and_flags) != 4:
        return None
    version = version_and_flags[0]
    if version == 0:
        fields = stream.read(16)
        if len(fields) != 16:
            return None
        _, _, timescale, duration = struct.unpack(">IIII", fields)
    elif version == 1:
        fields = stream.read(28)
        if len(fields) != 28:
            return None
        _, _, timescale, duration = struct.unpack(">QQIQ", fields)
    else:
        return None
    return timescale, duration


def get_mp4_media_info(
    path: str | os.PathLike[str],
) -> dict[str, float | int | str | bool | None]:
    """Read final-file properties needed by ``run_manifest.json``.

    The result contains ``duration_s``, ``resolution``, ``width``, ``height``,
    ``fps``, and ``audio_enabled``. Unknown values are returned as ``None``.
    """
    result: dict[str, float | int | str | bool | None] = {
        "duration_s": get_mp4_duration_seconds(path),
        "resolution": None,
        "width": None,
        "height": None,
        "fps": None,
        "audio_enabled": None,
    }
    try:
        file_size = os.path.getsize(path)
        with open(path, "rb") as stream:
            moov = next(
                (
                    (payload_start, box_end)
                    for box_type, payload_start, box_end in _iter_mp4_boxes(
                        stream, 0, file_size
                    )
                    if box_type == b"moov"
                ),
                None,
            )
            if moov is None:
                return result

            has_audio = False
            saw_track = False
            for box_type, trak_start, trak_end in _iter_mp4_boxes(
                stream, moov[0], moov[1]
            ):
                if box_type != b"trak":
                    continue
                saw_track = True
                handler_type: bytes | None = None
                width: int | None = None
                height: int | None = None
                timescale: int | None = None
                sample_count = 0
                sample_delta_total = 0

                for child_type, child_start, child_end in _iter_mp4_boxes(
                    stream, trak_start, trak_end
                ):
                    if child_type == b"tkhd" and child_end - child_start >= 8:
                        stream.seek(child_end - 8)
                        dimensions = stream.read(8)
                        if len(dimensions) == 8:
                            width_fixed, height_fixed = struct.unpack(
                                ">II", dimensions
                            )
                            width = round(width_fixed / 65536)
                            height = round(height_fixed / 65536)
                        continue
                    if child_type != b"mdia":
                        continue

                    for mdia_type, mdia_start, mdia_end in _iter_mp4_boxes(
                        stream, child_start, child_end
                    ):
                        if mdia_type == b"hdlr":
                            stream.seek(mdia_start + 8)
                            handler = stream.read(4)
                            if len(handler) == 4:
                                handler_type = handler
                        elif mdia_type == b"mdhd":
                            mdhd = _read_mdhd(stream, mdia_start)
                            if mdhd:
                                timescale, _ = mdhd
                        elif mdia_type == b"minf":
                            for minf_type, minf_start, minf_end in _iter_mp4_boxes(
                                stream, mdia_start, mdia_end
                            ):
                                if minf_type != b"stbl":
                                    continue
                                for (
                                    stbl_type,
                                    stbl_start,
                                    stbl_end,
                                ) in _iter_mp4_boxes(
                                    stream, minf_start, minf_end
                                ):
                                    if (
                                        stbl_type != b"stts"
                                        or stbl_end - stbl_start < 8
                                    ):
                                        continue
                                    stream.seek(stbl_start + 4)
                                    entry_data = stream.read(4)
                                    if len(entry_data) != 4:
                                        continue
                                    entry_count = struct.unpack(
                                        ">I", entry_data
                                    )[0]
                                    for _ in range(entry_count):
                                        entry = stream.read(8)
                                        if len(entry) != 8:
                                            break
                                        count, delta = struct.unpack(">II", entry)
                                        sample_count += count
                                        sample_delta_total += count * delta

                if handler_type == b"soun":
                    has_audio = True
                elif handler_type == b"vide":
                    if width and height:
                        result["width"] = width
                        result["height"] = height
                        result["resolution"] = f"{width}x{height}"
                    if timescale and sample_count and sample_delta_total:
                        result["fps"] = round(
                            sample_count * timescale / sample_delta_total, 3
                        )

            result["audio_enabled"] = has_audio if saw_track else None
    except (OSError, struct.error):
        return result

    return result
