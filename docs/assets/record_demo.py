#!/usr/bin/env python3
"""Record a 100% real terminal demo GIF from live cli-device / cli-device-d executions.

Runs actual commands against this repository's `.venv/bin/cli-device` and
`.venv/bin/cli-device-d`, stores the raw captured output in `demo-transcript.json`,
renders crisp dark-terminal frames with Pillow, and encodes an optimized looping GIF
with ffmpeg. Zero outputs are mocked or hand-edited.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

ASSETS_DIR = Path(__file__).resolve().parent
REPO_ROOT = ASSETS_DIR.parent.parent
VENV_BIN = REPO_ROOT / ".venv" / "bin"
CLI_BIN = str(VENV_BIN / "cli-device")
DAEMON_BIN = str(VENV_BIN / "cli-device-d")

FONT_MONO_REG = "/usr/share/fonts/truetype/sand-box/google/DM Mono/DMMono-Regular.ttf"
FONT_MONO_MED = "/usr/share/fonts/truetype/sand-box/google/DM Mono/DMMono-Medium.ttf"

# Canvas dimensions & theme (GitHub dark slate terminal)
WIDTH = 940
HEIGHT = 540
PAD_X = 28
TOP_BAR_H = 40
BODY_TOP = TOP_BAR_H + 18
LINE_H = 21
MAX_LINES = 22

BG_OUTER = (11, 16, 30)
BG_TERM = (13, 17, 23)
BG_BAR = (22, 27, 34)
BORDER = (48, 54, 61)
FG_DEFAULT = (230, 237, 243)
FG_MUTED = (139, 148, 158)
FG_PROMPT = (56, 189, 248)
FG_CMD = (248, 250, 252)
FG_OK = (52, 211, 153)
FG_FAIL = (248, 113, 113)
FG_WARN = (251, 191, 36)
FG_CYAN = (103, 232, 249)


def _capture_real_session() -> list[dict[str, Any]]:
    """Run real cli-device-d + cli-device commands and return raw captured steps."""
    steps: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="cli-device-demo-") as td:
        sock = Path(td) / "demo.sock"
        display_sock = "/tmp/cli-device.sock"

        daemon = subprocess.Popen(
            [DAEMON_BIN, "--bundle", "jubilee", "--socket", str(sock)],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5.0
            while not sock.exists():
                assert time.monotonic() < deadline, "cli-device-d socket did not appear"
                time.sleep(0.03)

            env = {**os.environ, "COLUMNS": "88"}
            commands: list[tuple[str, list[str], float, bool]] = [
                (
                    "cli-device --help",
                    [CLI_BIN, "--help"],
                    2.4,
                    True,
                ),
                (
                    "cli-device --bundle jubilee --help",
                    [CLI_BIN, "--bundle", "jubilee", "--help"],
                    2.4,
                    True,
                ),
                (
                    f"cli-device --bundle jubilee --target in_memory --socket {display_sock} sys detect",
                    [CLI_BIN, "--bundle", "jubilee", "--target", "in_memory", "--socket", str(sock), "sys", "detect"],
                    1.8,
                    True,
                ),
                (
                    f"cli-device --bundle jubilee --target in_memory --socket {display_sock} --dry-run motion home-all",
                    [CLI_BIN, "--bundle", "jubilee", "--target", "in_memory", "--socket", str(sock), "--dry-run", "motion", "home-all"],
                    2.4,
                    False,
                ),
                (
                    f"cli-device --bundle jubilee --target in_memory --socket {display_sock} motion home-all",
                    [CLI_BIN, "--bundle", "jubilee", "--target", "in_memory", "--socket", str(sock), "motion", "home-all"],
                    2.2,
                    False,
                ),
                (
                    f"cli-device --bundle jubilee --target in_memory --socket {display_sock} --confirm motion home-all",
                    [CLI_BIN, "--bundle", "jubilee", "--target", "in_memory", "--socket", str(sock), "--confirm", "motion", "home-all"],
                    2.2,
                    False,
                ),
                (
                    f"cli-device --bundle jubilee --target in_memory --socket {display_sock} --dry-run sys abort",
                    [CLI_BIN, "--bundle", "jubilee", "--target", "in_memory", "--socket", str(sock), "--dry-run", "sys", "abort"],
                    2.8,
                    False,
                ),
            ]

            for display_cmd, argv, hold_s, clear_before in commands:
                res = subprocess.run(
                    argv,
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                out = (res.stdout + res.stderr).replace(str(sock), display_sock).rstrip("\n")
                steps.append(
                    {
                        "command": display_cmd,
                        "exit_code": res.returncode,
                        "output": out,
                        "hold_s": hold_s,
                        "clear_before": clear_before,
                    }
                )
        finally:
            daemon.send_signal(signal.SIGTERM)
            daemon.wait(timeout=3.0)
            if daemon.stdout:
                daemon.stdout.close()
            if daemon.stderr:
                daemon.stderr.close()
    return steps


def _classify_line(line: str) -> tuple[str, tuple[int, int, int]]:
    stripped = line.lstrip()
    if line.startswith("$ "):
        return (line, FG_CMD)
    if stripped.startswith("[OK]"):
        return (line, FG_OK)
    if stripped.startswith("[FAIL]") or stripped.startswith("error:"):
        return (line, FG_FAIL)
    if stripped.startswith("hint:") or "physical power-cut" in line:
        return (line, FG_WARN)
    if "Let AI drive all your devices" in line:
        return (line, FG_CYAN)
    if stripped.startswith(("Device Commands", "Toolkit Commands", "Options:", "Usage:")):
        return (line, FG_PROMPT)
    if stripped.startswith(("plan:", "state:", "data:")):
        return (line, (186, 230, 253))
    return (line, FG_DEFAULT)


def _wrap_text(text: str, max_chars: int = 88) -> list[str]:
    wrapped: list[str] = []
    for raw in text.splitlines():
        if len(raw) <= max_chars:
            wrapped.append(raw)
        else:
            cur = raw
            while len(cur) > max_chars:
                wrapped.append(cur[:max_chars])
                cur = "  " + cur[max_chars:]
            if cur:
                wrapped.append(cur)
    return wrapped


def _render_frame(
    lines: list[tuple[str, tuple[int, int, int]]],
    *,
    font_reg: ImageFont.FreeTypeFont,
    font_med: ImageFont.FreeTypeFont,
    cursor_on: bool = False,
) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_OUTER)
    draw = ImageDraw.Draw(img)

    # Terminal window card
    draw.rounded_rectangle([8, 8, WIDTH - 9, HEIGHT - 9], radius=14, fill=BG_TERM, outline=BORDER, width=2)
    # Title bar
    draw.rounded_rectangle([10, 10, WIDTH - 11, TOP_BAR_H + 8], radius=12, fill=BG_BAR)
    draw.rectangle([10, TOP_BAR_H - 4, WIDTH - 11, TOP_BAR_H + 8], fill=BG_BAR)
    draw.line([10, TOP_BAR_H + 8, WIDTH - 11, TOP_BAR_H + 8], fill=BORDER, width=1)

    # Traffic lights
    draw.ellipse([26, 22, 38, 34], fill=(255, 95, 86))
    draw.ellipse([46, 22, 58, 34], fill=(255, 189, 46))
    draw.ellipse([66, 22, 78, 34], fill=(39, 201, 63))
    draw.text((WIDTH // 2 - 145, 20), "cli-device — offline in_memory demo", fill=FG_MUTED, font=font_reg)

    visible = lines[-MAX_LINES:]
    y = BODY_TOP + 6
    for idx, (text, color) in enumerate(visible):
        if text.startswith("$ "):
            draw.text((PAD_X, y), "$ ", fill=FG_PROMPT, font=font_med)
            cmd_part = text[2:]
            draw.text((PAD_X + 18, y), cmd_part, fill=FG_CMD, font=font_med)
            if cursor_on and idx == len(visible) - 1:
                bbox = draw.textbbox((PAD_X + 18, y), cmd_part, font=font_med)
                cx = min(bbox[2] + 3, WIDTH - PAD_X - 10)
                draw.rectangle([cx, y + 2, cx + 8, y + 17], fill=FG_OK)
        else:
            draw.text((PAD_X, y), text, fill=color, font=font_reg)
        y += LINE_H
    return img


def _build_gif(steps: list[dict[str, Any]], out_gif: Path) -> None:
    font_reg = ImageFont.truetype(FONT_MONO_REG, 14)
    font_med = ImageFont.truetype(FONT_MONO_MED, 14)

    fps = 10
    frames_dir = Path(tempfile.mkdtemp(prefix="cli-demo-frames-"))
    try:
        frame_idx = 0
        screen: list[tuple[str, tuple[int, int, int]]] = []

        def emit(img: Image.Image, count: int) -> None:
            nonlocal frame_idx
            for _ in range(count):
                img.save(frames_dir / f"frame_{frame_idx:05d}.png")
                frame_idx += 1

        for step in steps:
            if step["clear_before"]:
                screen = []
            elif screen:
                screen.append(("", FG_DEFAULT))

            cmd = str(step["command"])
            # Type command cleanly in chunks for snappy readability
            chunk = max(3, len(cmd) // 14)
            for end in range(chunk, len(cmd), chunk):
                typing = [*screen, (f"$ {cmd[:end]}", FG_CMD)]
                emit(_render_frame(typing, font_reg=font_reg, font_med=font_med, cursor_on=True), 1)
            screen.append((f"$ {cmd}", FG_CMD))
            emit(_render_frame(screen, font_reg=font_reg, font_med=font_med, cursor_on=False), 3)

            for raw_line in _wrap_text(str(step["output"]), max_chars=88):
                screen.append(_classify_line(raw_line))
            emit(
                _render_frame(screen, font_reg=font_reg, font_med=font_med, cursor_on=False),
                int(round(float(step["hold_s"]) * fps)),
            )

        palette = frames_dir / "palette.png"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-framerate",
                str(fps),
                "-i",
                str(frames_dir / "frame_%05d.png"),
                "-vf",
                "palettegen=max_colors=96:stats_mode=diff",
                str(palette),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-framerate",
                str(fps),
                "-i",
                str(frames_dir / "frame_%05d.png"),
                "-i",
                str(palette),
                "-lavfi",
                "paletteuse=dither=bayer:bayer_scale=3",
                "-loop",
                "0",
                str(out_gif),
            ],
            check=True,
            capture_output=True,
        )
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)


def main() -> None:
    steps = _capture_real_session()
    transcript_path = ASSETS_DIR / "demo-transcript.json"
    transcript_path.write_text(json.dumps(steps, indent=2) + "\n", encoding="utf-8")
    gif_path = ASSETS_DIR / "demo-terminal.gif"
    _build_gif(steps, gif_path)
    print(f"Captured {len(steps)} real commands -> {transcript_path}")
    print(f"Built GIF -> {gif_path} ({gif_path.stat().st_size / 1024:.1f} KiB)")


if __name__ == "__main__":
    main()
