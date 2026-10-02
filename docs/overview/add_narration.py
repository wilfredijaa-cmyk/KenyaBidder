"""Turns overview.webm + captions.json into overview.mp4 (H.264 + AAC) with a synthesized voice-over.

Needs espeak-ng (apt install espeak-ng) and imageio-ffmpeg (pip install imageio-ffmpeg) — the latter ships an ffmpeg with libx264.
The voice is a robotic offline TTS so the video is reproducible anywhere; replace the wav generation with a nicer TTS/human recording if you like.
"""
import json
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg

HERE = Path(__file__).parent
FF = imageio_ffmpeg.get_ffmpeg_exe()
caps = json.loads((HERE / "captions.json").read_text())
tmp = Path(tempfile.mkdtemp())
inputs, filters = [], []
for i, (at, text) in enumerate(caps):
    wav = tmp / f"{i}.wav"
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "-p", "45", "-w", str(wav), text.replace("→", "then").replace("…", "").replace("—", ",")], check=True)
    inputs += ["-i", str(wav)]
    filters.append(f"[{i + 1}:a]adelay={int((at + 0.3) * 1000)}|{int((at + 0.3) * 1000)}[a{i}]")
mix = "".join(f"[a{i}]" for i in range(len(caps))) + f"amix=inputs={len(caps)}:normalize=0:dropout_transition=0,volume=1.6[aout]"
subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(HERE / "overview.webm"), *inputs, "-filter_complex", ";".join(filters) + ";" + mix,
                "-map", "0:v", "-map", "[aout]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "24", "-preset", "medium", "-c:a", "aac", "-b:a", "96k",
                "-shortest", "-movflags", "+faststart", str(HERE / "overview.mp4")], check=True)
print("wrote", HERE / "overview.mp4")
