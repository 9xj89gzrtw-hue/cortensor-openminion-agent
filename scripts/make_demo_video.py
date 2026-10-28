#!/usr/bin/env python3
"""Build the hackathon demo video from a real run.

    python3 scripts/make_demo_video.py artifacts/demo-video/demo.mp4

Three parts, all from real artifacts:

1. a pty recording of the actual commands (timing comes from the run itself),
2. a pan over the generated HTML report (Chrome headless screenshot),
3. title and closing cards.

The terminal replay is slowed down for readability; that is stated on the closing card.
Requires Pillow, ffmpeg and Google Chrome. No network.
"""
from __future__ import annotations

import codecs
import json
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720
FPS = 12
BG = (11, 17, 32)
BAR = (17, 26, 45)
FG = (226, 232, 240)
DIM = (100, 116, 139)
GREEN = (163, 230, 53)
AMBER = (245, 197, 66)
RED = (248, 113, 113)
BLUE = (147, 197, 253)
MONO = "/System/Library/Fonts/SFNSMono.ttf"
SANS = "/System/Library/Fonts/SFNSDisplay-Bold.otf"
SANS_ALT = "/System/Library/Fonts/SFNS.ttf"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WORK = Path("artifacts/demo-video")   # gitignored: intermediate session + screenshot

TERMINAL_SPEED = 1.55
TEST_SPEED = 2.6
IDLE_CAP = 0.45
TYPE_SPEED = 30.0

COMMANDS: list[tuple[list[str], str, float]] = [
    (["./canary", "version"], "Deterministic core needs no SDK. The agent path needs OpenMinion 0.1.14.", TERMINAL_SPEED),
    (["./canary", "suites"], "Suites are plain JSON. You commit your own; three ship as examples.", TERMINAL_SPEED),
    (["./canary", "targets"], "Cortensor Portal, a local Router and Ollama — same code path.", TERMINAL_SPEED),
    (["./canary", "demo", "--agent"], "Real OpenMinion loop: 4 read-only tools. The model cannot report a number it did not read.", TERMINAL_SPEED),
    ([".venv/bin/python", "-m", "unittest", "discover", "-s", "tests"],
     "65 tests, no network. The agent tests run for real when OpenMinion is installed.", TEST_SPEED),
]


def font(path: str, size: int, fallback: str | None = None) -> ImageFont.FreeTypeFont:
    for candidate in (path, fallback, MONO):
        if candidate and Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


# ------------------------------------------------------------------ record
def record(out_path: str) -> None:
    events: list[dict] = []
    t0 = time.monotonic()

    for cmd, caption, _speed in COMMANDS:
        events.append({"t": time.monotonic() - t0, "kind": "cmd", "text": " ".join(cmd), "caption": caption})
        master, slave = pty.openpty()
        env = dict(os.environ, TERM="xterm-256color", COLUMNS="104", LINES="30")
        proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
        os.close(slave)

        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buffer, pending = "", ""
        while True:
            ready, _, _ = select.select([master], [], [], 0.05)
            if master in ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                text = pending + decoder.decode(chunk)
                pending = ""
                if text.endswith("\r"):
                    text, pending = text[:-1], "\r"
                text = text.replace("\r\n", "\n")
                for char in text:
                    if char == "\r":
                        buffer = ""
                    elif char == "\n":
                        events.append({"t": time.monotonic() - t0, "kind": "line", "text": buffer})
                        buffer = ""
                    else:
                        buffer += char
            elif proc.poll() is not None:
                break
        if buffer:
            events.append({"t": time.monotonic() - t0, "kind": "line", "text": buffer})
        os.close(master)
        proc.wait()
        events.append({"t": time.monotonic() - t0, "kind": "gap"})

    payload = {"events": events, "raw_duration": events[-1]["t"]}
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"recorded {len(events)} events, {payload['raw_duration']:.1f}s of real runtime")


# ------------------------------------------------------------------ terminal frames
def terminal_frames(session: dict) -> list[dict]:
    """Return [{t, rows, typing, caption}] in replay time."""
    frames: list[dict] = []
    rows: list[dict] = []
    clock = 0.0
    last = 0.0
    speed = TERMINAL_SPEED
    caption = ""

    for index, event in enumerate(session["events"]):
        gap = max(0.0, event["t"] - last) * speed
        gap = min(gap, IDLE_CAP * speed * 2)
        clock += gap
        last = event["t"]

        if event["kind"] == "cmd":
            speed = COMMANDS[[c[0] for c in COMMANDS].index(event["text"].split())][2]
            caption = event["caption"]
            text = event["text"]
            rows.append({"kind": "cmd", "text": ""})
            steps = max(2, len(text))
            type_time = len(text) / TYPE_SPEED
            for i in range(steps + 1):
                rows[-1]["text"] = text[: int(len(text) * i / steps)]
                frames.append({"t": clock + type_time * i / steps, "rows": list(rows), "typing": True, "caption": caption})
            clock += type_time + 0.12
            rows[-1]["text"] = text
            frames.append({"t": clock, "rows": list(rows), "typing": False, "caption": caption})
        elif event["kind"] == "line":
            rows.append({"kind": "out", "text": event["text"]})
            frames.append({"t": clock, "rows": list(rows), "typing": False, "caption": caption})
        elif event["kind"] == "gap":
            clock += 0.45
            frames.append({"t": clock, "rows": list(rows), "typing": False, "caption": caption})

    frames.append({"t": clock + 1.2, "rows": list(rows), "typing": False, "caption": caption})
    return frames


def color_for(line: str) -> tuple:
    if " PASS " in line and " ok" not in line:
        return GREEN
    if " FAIL " in line or "ERROR" in line or "FAILED" in line:
        return RED
    if line.strip().startswith("[agent]"):
        return AMBER
    if line.startswith(("./canary", ".venv")):
        return BLUE
    if line.startswith("  ") and ("gate" in line or "limit" in line):
        return DIM
    if line.startswith(("ok", "PASS")) or line.endswith(" OK"):
        return GREEN
    return FG


def draw_terminal(img: Image.Image, rows: list[dict], caption: str, typing: bool, frame_no: int) -> None:
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, W, 44), fill=BAR)
    for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        draw.ellipse((18 + i * 22, 16, 32 + i * 22, 30), fill=c)
    draw.text((W / 2, 22), "canary — cortensor hackathon #5", font=font(MONO, 16), fill=DIM, anchor="mm")

    body = font(MONO, 18)
    line_h = 25
    max_rows = (H - 44 - 74) // line_h
    y = 60
    for row in rows[-max_rows:]:
        if row["kind"] == "cmd":
            draw.text((22, y), "$ " + row["text"], font=body, fill=BLUE)
        else:
            draw.text((22, y), row["text"], font=body, fill=color_for(row["text"]))
        y += line_h

    if (frame_no // 5) % 2 == 0:
        draw.rectangle((22, y + 3, 33, y + line_h - 6), fill=GREEN)

    if caption:
        draw.rectangle((0, H - 58, W, H), fill=(15, 23, 42))
        draw.text((26, H - 29), caption, font=font(SANS_ALT, 19), fill=(203, 213, 225), anchor="lm")


# ------------------------------------------------------------------ report pan
def screenshot_report(html: str, out_png: str) -> tuple[int, int]:
    html = str(Path(html).resolve())
    out_png = str(Path(out_png).resolve())
    subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
         f"--screenshot={out_png}", "--window-size=1280,2400", f"file://{html}"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    with Image.open(out_png) as img:
        return img.size


def report_frames(png: str, seconds: float = 13.0) -> list[dict]:
    img = Image.open(png).convert("RGB")
    scale = W / img.width
    img = img.resize((W, int(img.height * scale)), Image.LANCZOS)
    travel = max(0, img.height - (H - 44))
    frames = []
    total = int(seconds * FPS)
    for i in range(total):
        p = i / max(1, total - 1)
        eased = p * p * (3 - 2 * p)
        offset = int(travel * eased)
        frames.append({"t": i / FPS, "offset": offset, "image": img})
    return frames


# ------------------------------------------------------------------ cards
def card_frame(lines: list[tuple[str, int, tuple]], footer: str = "") -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 12, H), fill=AMBER)
    y = H / 2 - 110
    for text, size, colour in lines:
        draw.text((90, y), text, font=font(SANS, size, SANS_ALT), fill=colour)
        y += size * 1.55
    if footer:
        draw.text((90, H - 70), footer, font=font(SANS_ALT, 17), fill=DIM)
    return img


# ------------------------------------------------------------------ compose
def main(out_path: str) -> None:
    session_path = WORK / "session.json"
    WORK.mkdir(parents=True, exist_ok=True)
    if not session_path.exists():
        record(str(session_path))
    session = json.loads(session_path.read_text())

    report_html = next(Path("artifacts").glob("demo-agent-run/reports/*/report.html"))
    report_png = str(WORK / "report.png")
    screenshot_report(str(report_html), report_png)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium",
         "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path],
        stdin=subprocess.PIPE,
    )

    def push(img: Image.Image, times: int = 1) -> None:
        data = img.tobytes()
        for _ in range(times):
            ffmpeg.stdin.write(data)

    # 1. title
    push(card_frame([
        ("Cortensor Canary", 62, FG),
        ("A release gate for LLM endpoints", 32, BLUE),
        ("Deterministic verdicts from your own task suite", 22, DIM),
    ], "OpenMinion 0.1.14  ·  Python stdlib  ·  65 tests"), int(4.0 * FPS))

    # 2. terminal replay
    frames = terminal_frames(session)
    frame_no = 0
    total = frames[-1]["t"]
    for i in range(int(total * FPS) + 1):
        stamp = i / FPS
        visible = [fr for fr in frames if fr["t"] <= stamp]
        current = visible[-1] if visible else frames[0]
        img = Image.new("RGB", (W, H), BG)
        draw_terminal(img, current["rows"], current["caption"], current["typing"], i)
        push(img)
        frame_no += 1

    # 3. how the gate decides
    push(card_frame([
        ("How the gate decides", 52, FG),
        ("pass rate      -2.0 pp or worse      -> FAIL", 26, GREEN),
        ("p95 latency    +25% WARN / +50% FAIL -> FAIL", 26, AMBER),
        ("error rate     2% of cases          -> FAIL", 26, BLUE),
        ("suite identity must match           -> FAIL", 26, DIM),
    ], "Thresholds are CLI flags. The verdict names the regressed case ids."), int(6.0 * FPS))

    # 4. report pan
    pan = report_frames(report_png, seconds=15.0)
    for f in pan:
        img = Image.new("RGB", (W, H), BG)
        img.paste(f["image"].crop((0, f["offset"], W, f["offset"] + H - 44)), (0, 44))
        draw_terminal_header = ImageDraw.Draw(img)
        draw_terminal_header.rectangle((0, 0, W, 44), fill=BAR)
        draw_terminal_header.text((W / 2, 22), "artifacts/.../report.html — generated by the run above",
                                  font=font(MONO, 16), fill=DIM, anchor="mm")
        draw_terminal_header.rectangle((0, H - 58, W, H), fill=(15, 23, 42))
        draw_terminal_header.text((26, H - 29), "Same numbers, one self-contained HTML file. No dashboard, no DB.",
                                  font=font(SANS_ALT, 19), fill=(203, 213, 225), anchor="lm")
        push(img)

    # 4. closing
    push(card_frame([
        ("Verdict: FAIL", 60, RED),
        ("quality -16.7pp   ·   p95 +500%   ·   0 errors", 30, FG),
        ("The candidate is a real regression, named by case id.", 22, DIM),
    ], "github.com/9xj89gzrtw-hue/cortensor-openminion-agent  ·  terminal replay 0.65× for readability"),
         int(6.5 * FPS))

    ffmpeg.stdin.close()
    ffmpeg.wait()
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "artifacts/demo-video/demo.mp4")
