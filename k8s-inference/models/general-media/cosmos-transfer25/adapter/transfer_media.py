"""Transfer NIM media validation, independent of legacy Cosmos3 restrictions."""
from pathlib import Path


def inspect_video(path: Path) -> dict:
    import av

    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular MP4")
    with path.open("rb") as handle:
        if b"ftyp" not in handle.read(32):
            raise ValueError("Expected an MP4 container; no conversion performed")
    with av.open(str(path)) as source:
        if len(source.streams.video) != 1:
            raise ValueError("Select one unambiguous video stream")
        stream = source.streams.video[0]
        frames = 0
        for _frame in source.decode(video=0):
            frames += 1
            if frames > 480:
                raise ValueError("Transfer NIM supports 93–480 frames; not trimmed")
        if frames < 93:
            raise ValueError("Transfer NIM supports 93–480 frames; not padded")
        return {"width": stream.width, "height": stream.height, "frames": frames,
                "fps": str(stream.average_rate) if stream.average_rate is not None else None,
                "audio_streams": len(source.streams.audio)}
