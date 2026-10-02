# KenyaBidder — overview

* **`overview.mp4`** — the same walkthrough as H.264 + AAC **with a synthesized voice-over** (offline espeak-ng voice, so it sounds robotic — swap in a human/neural voice if you want polish).
* **`overview.webm`** — silent ~3-minute captioned walkthrough recorded from the real app: sign-up (EN/SW), agent rules and brain, a listing → agent bid → match → contact reveal,
  a reverse auction (RFQ), wallet, profile/verification, and the admin console. 
* **`wireframes/`** — low-fidelity SVG wireframes of the main screens plus the bid-path architecture (`index.html` shows them all).
* **`screens/`** — real screenshots taken during the recording.

Regenerate: `python docs/overview/make_wireframes.py`; `python docs/overview/make_video.py` (Playwright + Chromium, ~4 minutes; also writes `captions.json`); then `python docs/overview/add_narration.py` (needs `apt install espeak-ng` and `pip install imageio-ffmpeg`) to produce the narrated MP4.
