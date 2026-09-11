#!/usr/bin/env python3
"""
Seed demo avatars (and optionally demo voices) so a fresh install gets a
"talk to an avatar" moment in under two minutes — no photo hunting required.

Usage:
    python scripts/seed_demo.py                 # 3 demo avatars
    python scripts/seed_demo.py --with-voices   # + a cloned demo voice each
    python scripts/seed_demo.py --api http://localhost:8000
    python scripts/seed_demo.py --token "$JWT"  # seed into a real account

With no --token the script claims a throwaway guest account and seeds into
that, so it works against a fresh install with no signup step.

Avatars use AI-generated faces from thispersondoesnotexist.com (no real
person, no copyright). If that service is unreachable, a stylized placeholder
face is generated locally with Pillow instead.

Requires: `pip install requests` (already in backend/requirements.txt — the
backend venv works: `backend/venv/bin/python scripts/seed_demo.py`).
"""

from __future__ import annotations

import argparse
import io
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("This script needs `requests`. Run: pip install requests")

FACE_SOURCE = "https://thispersondoesnotexist.com"

# Name, personality system prompt, voice line (synthesized for --with-voices)
DEMO_AVATARS = [
    {
        "name": "Nova — Tech Mentor",
        "system_prompt": (
            "You are Nova, a friendly and endlessly curious tech mentor. "
            "Explain things simply with vivid analogies, stay encouraging, "
            "and keep answers short enough to speak aloud."
        ),
    },
    {
        "name": "Sage — Calm Philosopher",
        "system_prompt": (
            "You are Sage, a calm and thoughtful philosopher. You speak "
            "slowly and deliberately, often answering questions with a "
            "gentle counter-question. Keep replies brief and contemplative."
        ),
    },
    {
        "name": "Spark — Upbeat Comedian",
        "system_prompt": (
            "You are Spark, an upbeat stand-up comedian. You find the funny "
            "side of everything and love wordplay, but never at the user's "
            "expense. Keep replies punchy — one or two lines."
        ),
    },
]

VOICE_SAMPLE_TEXT = (
    "Hi there! I'm one of the demo voices for the AvatarAI platform. "
    "This sample is about fifteen seconds long, which is exactly what the "
    "voice cloning engine needs to capture how I sound. Pretty neat, right?"
)


def fetch_face(timeout: int = 15) -> bytes:
    """AI-generated face (not a real person). Raises on failure."""
    resp = requests.get(FACE_SOURCE, timeout=timeout, headers={"User-Agent": "avatarai-seed"})
    resp.raise_for_status()
    if not resp.headers.get("content-type", "").startswith("image/"):
        raise RuntimeError(f"Unexpected content type: {resp.headers.get('content-type')}")
    return resp.content


def placeholder_face(label: str) -> bytes:
    """Offline fallback: a stylized face good enough for the `simple` engine."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (512, 512), (38, 33, 56))
    d = ImageDraw.Draw(img)
    d.ellipse((96, 64, 416, 448), fill=(224, 188, 154))  # head
    d.ellipse((176, 200, 224, 248), fill=(40, 40, 60))  # left eye
    d.ellipse((288, 200, 336, 248), fill=(40, 40, 60))  # right eye
    d.arc((192, 280, 320, 392), start=20, end=160, fill=(150, 80, 70), width=10)  # smile
    d.text((20, 470), f"AvatarAI demo — {label}", fill=(200, 200, 220))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def synthesize_voice_sample() -> bytes | None:
    """~15s WAV via gTTS for --with-voices. Returns None if unavailable."""
    try:
        from gtts import gTTS
        from pydub import AudioSegment

        mp3 = io.BytesIO()
        gTTS(text=VOICE_SAMPLE_TEXT, lang="en").write_to_fp(mp3)
        mp3.seek(0)
        wav = io.BytesIO()
        AudioSegment.from_mp3(mp3).export(wav, format="wav")
        return wav.getvalue()
    except Exception as e:  # noqa: BLE001 — any failure just skips voices
        print(f"  ! could not synthesize voice sample ({e}); skipping voices")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default="http://localhost:8000", help="Backend base URL")
    ap.add_argument("--with-voices", action="store_true", help="Also clone a demo voice")
    ap.add_argument(
        "--token",
        default=None,
        help="Bearer token. Omitted, the script creates a throwaway guest account.",
    )
    args = ap.parse_args()

    s = requests.Session()

    # Sanity: backend reachable?
    try:
        health = s.get(f"{args.api}/health", timeout=10).json()
        print(f"Backend OK ({health.get('environment')}, status={health.get('status')})")
    except Exception as e:
        sys.exit(f"Backend not reachable at {args.api} — is it running? ({e})")

    # Every resource endpoint requires an authenticated identity — there is no
    # anonymous fallback to a shared demo account any more. With no token
    # supplied, claim a guest account so the script still works out of the box;
    # the seeded avatars then belong to that guest and are reaped with it.
    if args.token:
        s.headers["Authorization"] = f"Bearer {args.token}"
    else:
        try:
            r = s.post(f"{args.api}/api/v1/users/guest", timeout=15)
            r.raise_for_status()
            s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
            print("Using a throwaway guest account (pass --token to seed a real one)")
        except Exception as e:
            sys.exit(
                f"Could not create a guest account ({e}).\n"
                f"Either enable guests (GUEST_ACCOUNTS_ENABLED=true) or pass "
                f"--token with a bearer token from POST /api/v1/users/login."
            )

    existing = {a["name"] for a in s.get(f"{args.api}/api/v1/avatars/", timeout=15).json()}

    for spec in DEMO_AVATARS:
        name = spec["name"]
        if name in existing:
            print(f"= {name} already exists, skipping")
            continue

        try:
            face = fetch_face()
            source = "thispersondoesnotexist.com"
        except Exception as e:
            print(f"  ! face download failed ({e}); using local placeholder")
            face = placeholder_face(name.split(" ")[0])
            source = "placeholder"

        resp = s.post(
            f"{args.api}/api/v1/avatars/upload",
            files={"file": ("demo.jpg", face, "image/jpeg")},
            data={"name": name},
            timeout=120,
        )
        if resp.status_code != 201:
            print(f"  ✗ upload failed for {name}: {resp.status_code} {resp.text[:200]}")
            continue
        avatar = resp.json()
        print(f"✓ {name} created (face: {source})")

        # Personality
        meta = s.patch(
            f"{args.api}/api/v1/avatars/{avatar['id']}/metadata",
            json={"system_prompt": spec["system_prompt"]},
            timeout=30,
        )
        if meta.status_code == 200:
            print("  ✓ personality set")

        if args.with_voices:
            sample = synthesize_voice_sample()
            if sample:
                voice = s.post(
                    f"{args.api}/api/v1/voices/clone",
                    files={"audio": ("demo_voice.wav", sample, "audio/wav")},
                    data={"name": f"{name.split(' ')[0]} Voice", "language": "en"},
                    timeout=120,
                )
                if voice.status_code == 200:
                    vid = voice.json()["id"]
                    s.put(
                        f"{args.api}/api/v1/avatars/{avatar['id']}/voice",
                        params={"voice_id": vid},
                        timeout=30,
                    )
                    print("  ✓ demo voice cloned + assigned")
                else:
                    print(f"  ! voice clone failed: {voice.status_code} {voice.text[:150]}")

        time.sleep(1)  # be polite to the face source

    print("\nDone. Open http://localhost:3000 → Avatars → pick one → Start Conversation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
