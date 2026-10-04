"""Bounded, in-memory GIF construction for the Comment exhibit editor.

This module deliberately accepts image *bytes*, never a path or a command.  The
upload route remains responsible for its normal authentication and media
validation; callers can use :func:`build_gif_result` to show whether optional
optical smoothing was actually available before saving the returned bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
from typing import Iterable

from PIL import Image, ImageOps, UnidentifiedImageError

try:  # Optional: the normal editor must still work without OpenCV.
    import cv2 as _cv2
    import numpy as _np
except ImportError:  # pragma: no cover - exercised by monkeypatch below
    _cv2 = None
    _np = None


MAX_INPUT_FRAMES = 8
MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_SOURCE_PIXELS = 16_000_000
MAX_OUTPUT_PIXELS = 64_000_000
MAX_OUTPUT_FRAMES = 120
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
MAX_SIDE = 512
ALPHA_THRESHOLD = 128

_SPEED_MS = {"slow": 150, "normal": 95, "fast": 60}
_SIDE_ATTEMPTS = (512, 480, 448, 416, 384, 352, 320, 288, 256)
_COLOUR_ATTEMPTS = (128, 96, 80, 64)


@dataclass(frozen=True)
class GifBuildResult:
    """The finished GIF plus facts the UI may disclose without guessing."""

    data: bytes
    mode: str  # ``smooth`` only when OpenCV interpolation actually ran.
    frame_count: int
    durations_ms: tuple[int, ...]


def _validate_frame_bytes(frames: list[bytes]) -> None:
    if not 2 <= len(frames) <= MAX_INPUT_FRAMES:
        raise ValueError(f"需要 {2} 到 {MAX_INPUT_FRAMES} 张静态图片")
    if any(not isinstance(frame, bytes) or not frame for frame in frames):
        raise ValueError("每一帧必须是非空图片字节")
    if sum(map(len, frames)) > MAX_INPUT_BYTES:
        raise ValueError("全部原图总大小不能超过 10 MB")


def _decode_static(frame: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(frame)) as source:
            if source.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("只支持 PNG、JPG 或静态 WebP 图片")
            if getattr(source, "n_frames", 1) != 1:
                raise ValueError("请上传静态图片，不支持把已有动画再嵌入 GIF")
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > MAX_SOURCE_PIXELS:
                raise ValueError("单张图片像素过大")
            # ``load`` happens while PIL's file object is open; returning a copy
            # also makes later processing independent from untrusted input bytes.
            source.load()
            normalized=ImageOps.exif_transpose(source).convert("RGBA")
            normalized.thumbnail((MAX_SIDE,MAX_SIDE),Image.Resampling.LANCZOS)
            return normalized.copy()
    except UnidentifiedImageError as exc:
        raise ValueError("包含无法识别的图片") from exc


def _resize_to_canvas(images: Iterable[Image.Image], max_side: int) -> list[Image.Image]:
    resized: list[Image.Image] = []
    for image in images:
        width, height = image.size
        scale = min(1.0, max_side / max(width, height))
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        resized.append(image if size == image.size else image.resize(size, Image.Resampling.LANCZOS))

    canvas_size = (max(image.width for image in resized), max(image.height for image in resized))
    if canvas_size[0] * canvas_size[1] * len(resized) > MAX_OUTPUT_PIXELS:
        raise ValueError("图片总像素过大")
    result: list[Image.Image] = []
    for image in resized:
        canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        canvas.alpha_composite(image, ((canvas_size[0] - image.width) // 2, (canvas_size[1] - image.height) // 2))
        result.append(canvas)
    return result


def _sequence(images: list[Image.Image], ping_pong: bool) -> list[Image.Image]:
    return images + images[-2:0:-1] if ping_pong and len(images) > 2 else list(images)


def _premultiplied_midpoint(left: Image.Image, right: Image.Image) -> Image.Image:
    """One OpenCV flow midpoint, preserving premultiplied RGB and alpha.

    Transparent pixels are represented as zero, rather than composited against a
    white matte.  GIF itself has binary alpha, so quantisation later intentionally
    maps alpha below ``ALPHA_THRESHOLD`` to its transparent palette entry.
    """
    assert _cv2 is not None and _np is not None
    left_rgba = _np.asarray(left, dtype=_np.uint8)
    right_rgba = _np.asarray(right, dtype=_np.uint8)
    left_alpha = left_rgba[..., 3].astype(_np.float32) / 255.0
    right_alpha = right_rgba[..., 3].astype(_np.float32) / 255.0
    left_pre = left_rgba[..., :3].astype(_np.float32) * left_alpha[..., None]
    right_pre = right_rgba[..., :3].astype(_np.float32) * right_alpha[..., None]
    # A premultiplied grayscale is stable at a transparent boundary and does not
    # create the white halo produced by the original desktop helper's matte.
    left_gray = _cv2.cvtColor(_np.clip(left_pre, 0, 255).astype(_np.uint8), _cv2.COLOR_RGB2GRAY)
    right_gray = _cv2.cvtColor(_np.clip(right_pre, 0, 255).astype(_np.uint8), _cv2.COLOR_RGB2GRAY)
    flow_lr = _cv2.calcOpticalFlowFarneback(left_gray, right_gray, None, 0.5, 3, 21, 3, 5, 1.2, 0)
    flow_rl = _cv2.calcOpticalFlowFarneback(right_gray, left_gray, None, 0.5, 3, 21, 3, 5, 1.2, 0)
    height, width = left_gray.shape
    grid_x, grid_y = _np.meshgrid(_np.arange(width), _np.arange(height))

    def warp(array, flow, factor):
        map_x = (grid_x - flow[..., 0] * factor).astype(_np.float32)
        map_y = (grid_y - flow[..., 1] * factor).astype(_np.float32)
        return _cv2.remap(array, map_x, map_y, _cv2.INTER_LINEAR, borderMode=_cv2.BORDER_CONSTANT, borderValue=0)

    l_pre = warp(left_pre, flow_lr, 0.5)
    r_pre = warp(right_pre, flow_rl, 0.5)
    l_alpha = warp(left_alpha, flow_lr, 0.5)
    r_alpha = warp(right_alpha, flow_rl, 0.5)
    alpha = _np.clip((l_alpha + r_alpha) / 2.0, 0.0, 1.0)
    premul = (l_pre + r_pre) / 2.0
    rgb = _np.zeros_like(premul)
    visible = alpha > 1e-6
    rgb[visible] = premul[visible] / alpha[visible, None]
    rgba = _np.empty((height, width, 4), dtype=_np.uint8)
    rgba[..., :3] = _np.clip(rgb, 0, 255).astype(_np.uint8)
    rgba[..., 3] = _np.round(alpha * 255).astype(_np.uint8)
    return Image.fromarray(rgba, "RGBA")


def _animation(images: list[Image.Image], *, ping_pong: bool, smooth: bool, speed: str) -> tuple[list[Image.Image], list[int], str]:
    sequence = _sequence(images, ping_pong)
    use_smoothing = smooth and _cv2 is not None and _np is not None
    output: list[Image.Image] = []
    duration = _SPEED_MS[speed]
    for index, image in enumerate(sequence[:-1]):
        output.append(image)
        if use_smoothing:
            output.append(_premultiplied_midpoint(image, sequence[index + 1]))
    output.append(sequence[-1])
    if len(output) > MAX_OUTPUT_FRAMES:
        raise ValueError("动画帧数超过上限")
    durations = [duration] * len(output)
    # Make the end of the forward journey readable without affecting loop or
    # disposal semantics.  This is intentionally a duration change, not a frame
    # duplication, so it cannot exceed the frame ceiling.
    forward_end = (len(images) - 1) * (2 if use_smoothing else 1)
    durations[forward_end] = max(duration, 420)
    return output, durations, "smooth" if use_smoothing else "frames"


def _palette_frame(image: Image.Image, colours: int) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    # Do not matte-compose RGB.  The transparent palette index is reserved;
    # partially transparent source pixels are intentionally made binary for GIF.
    palette = rgba.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE, colors=max(2, colours - 1))
    if _np is not None:
        indexes = _np.asarray(palette, dtype=_np.uint8).copy() + 1
        transparent = _np.asarray(alpha, dtype=_np.uint8) < ALPHA_THRESHOLD
        indexes[transparent] = 0
        output = Image.fromarray(indexes, "P")
    else:  # Tiny dependency-free fallback; slower but bounded at 512px.
        source = list(palette.get_flattened_data())
        mask = list(alpha.get_flattened_data())
        output = Image.new("P", palette.size)
        output.putdata([0 if alpha_value < ALPHA_THRESHOLD else palette_index + 1 for alpha_value, palette_index in zip(mask, source)])
    source_palette = (palette.getpalette() or [])[: (colours - 1) * 3]
    output.putpalette(([0, 0, 0] + source_palette + [0] * 768)[:768])
    output.info["transparency"] = 0
    return output


def _encode(frames: list[Image.Image], durations: list[int], colours: int) -> bytes:
    prepared = [_palette_frame(frame, colours) for frame in frames]
    target = io.BytesIO()
    prepared[0].save(
        target,
        format="GIF",
        save_all=True,
        append_images=prepared[1:],
        duration=durations,
        loop=0,
        disposal=2,
        transparency=0,
        optimize=True,
    )
    return target.getvalue()


def build_gif_result(frames: list[bytes], speed: str = "normal", ping_pong: bool = True, smooth: bool = True) -> GifBuildResult:
    """Build a safe exhibit GIF entirely in memory.

    ``smooth=True`` requests one optical-flow midpoint per segment.  If OpenCV
    (and NumPy) are absent or smoothing is disabled, the result explicitly says
    ``mode='frames'`` and contains the original keyframes only.
    """
    if speed not in _SPEED_MS:
        raise ValueError("速度只能是 slow、normal 或 fast")
    _validate_frame_bytes(frames)
    source = [_decode_static(frame) for frame in frames]
    for side in _SIDE_ATTEMPTS:
        canvas = _resize_to_canvas(source, side)
        animation, durations, mode = _animation(canvas, ping_pong=ping_pong, smooth=smooth, speed=speed)
        if animation[0].width * animation[0].height * len(animation) > MAX_OUTPUT_PIXELS:
            continue
        for colours in _COLOUR_ATTEMPTS:
            encoded = _encode(animation, durations, colours)
            if len(encoded) <= MAX_OUTPUT_BYTES:
                return GifBuildResult(encoded, mode, len(animation), tuple(durations))
    # Do not return an over-limit blob for a later upload layer to accidentally
    # persist.  ``smallest`` is deliberately not exposed in the error.
    raise ValueError("生成结果仍超过 10 MB，请减少图片细节或尺寸")


def build_gif(frames: list[bytes], speed: str = "normal", ping_pong: bool = True, smooth: bool = True) -> bytes:
    """Convenience API for callers that only need uploadable GIF bytes."""
    return build_gif_result(frames, speed=speed, ping_pong=ping_pong, smooth=smooth).data
