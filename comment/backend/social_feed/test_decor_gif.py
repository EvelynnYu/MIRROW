"""Pure unit tests for the in-memory exhibit GIF builder."""

import io

from PIL import Image
import pytest

from social_feed import decor_gif


def _image_bytes(colour, *, size=(32, 24), alpha=255, fmt="PNG"):
    raw = io.BytesIO()
    Image.new("RGBA", size, (*colour, alpha)).save(raw, format=fmt)
    return raw.getvalue()


def _animation_bytes():
    raw = io.BytesIO()
    first = Image.new("RGBA", (8, 8), (255, 0, 0, 255))
    second = Image.new("RGBA", (8, 8), (0, 255, 0, 255))
    first.save(raw, format="WEBP", save_all=True, append_images=[second], duration=100)
    return raw.getvalue()


def test_gif_rejects_bad_frame_counts_and_oversize_input():
    one = _image_bytes((1, 2, 3))
    with pytest.raises(ValueError, match="2 到 8"):
        decor_gif.build_gif([one])
    with pytest.raises(ValueError, match="10 MB"):
        decor_gif.build_gif([b"a" * (6 * 1024 * 1024), b"b" * (6 * 1024 * 1024)])
    with pytest.raises(ValueError, match="静态"):
        decor_gif.build_gif([_animation_bytes(), one])


def test_gif_keeps_transparency_loop_duration_and_canvas_centering():
    transparent = _image_bytes((255, 0, 0), size=(40, 20), alpha=0)
    solid = _image_bytes((0, 0, 255), size=(20, 40))
    result = decor_gif.build_gif_result([transparent, solid], speed="fast", ping_pong=False, smooth=False)
    assert len(result.data) <= decor_gif.MAX_OUTPUT_BYTES
    image = Image.open(io.BytesIO(result.data))
    assert image.size == (40, 40)
    assert image.info["loop"] == 0
    assert image.info["transparency"] == 0
    assert image.n_frames == result.frame_count == 2
    image.seek(0)
    assert image.info["duration"] == 60
    image.seek(1)
    assert image.info["duration"] >= 420
    assert image.convert("RGBA").getpixel((0, 0))[3] == 0


def test_gif_real_fallback_without_opencv(monkeypatch):
    monkeypatch.setattr(decor_gif, "_cv2", None)
    monkeypatch.setattr(decor_gif, "_np", None)
    result = decor_gif.build_gif_result([_image_bytes((255, 0, 0)), _image_bytes((0, 0, 255))], smooth=True)
    assert result.mode == "frames"
    image = Image.open(io.BytesIO(result.data))
    assert image.n_frames == 2


def test_gif_output_is_bounded_even_when_smoothing_is_requested():
    frames = [_image_bytes((index * 20, 0, 255 - index * 20), size=(512, 512)) for index in range(8)]
    result = decor_gif.build_gif_result(frames, smooth=True)
    assert result.frame_count <= decor_gif.MAX_OUTPUT_FRAMES
    assert len(result.data) <= decor_gif.MAX_OUTPUT_BYTES
