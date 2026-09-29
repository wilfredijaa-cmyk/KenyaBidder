# KenyaBidder — overview

* **`overview.webm`** — ~2-minute captioned walkthrough recorded from the real app: sign-up (EN/SW), agent rules and brain, a listing → agent bid → match → contact reveal,
  a reverse auction (RFQ), wallet, profile/verification, and the admin console. (WebM plays in any modern browser; the sandbox that recorded it had no H.264 encoder.)
* **`wireframes/`** — low-fidelity SVG wireframes of the main screens plus the bid-path architecture (`index.html` shows them all).
* **`screens/`** — real screenshots taken during the recording.

Regenerate: `python docs/overview/make_wireframes.py` and `python docs/overview/make_video.py` (needs Playwright + Chromium; takes ~3 minutes).
