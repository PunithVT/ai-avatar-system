"""
Account and media lifecycle: what gets deleted, when, and who may change what.

Each test pins a defect found by audit — generated media that was never
removed, a guest who "signed up" and was deleted anyway, a password that could
be changed without knowing it, and a voice that could be attached without
owning it.
"""

import os
import time
from pathlib import Path

import pytest
from httpx import AsyncClient

import app.api.v1.voices as voices_module
import app.services.storage as storage_module
from app.celery_app import _sweep_local_videos
from app.config import settings
from app.services.avatar_processor import AvatarProcessor
from app.services.storage import LocalStorageService


async def _guest(client: AsyncClient) -> dict:
    token = (await client.post("/api/v1/users/guest")).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ── passwords ────────────────────────────────────────────────────────────
async def test_password_change_requires_the_current_password(
    client: AsyncClient, test_user, auth_headers
):
    r = await client.put(
        "/api/v1/users/me", headers=auth_headers, json={"password": "NewPassw0rd!x"}
    )
    assert r.status_code == 403

    r = await client.put(
        "/api/v1/users/me",
        headers=auth_headers,
        json={"password": "NewPassw0rd!x", "current_password": "definitely-wrong"},
    )
    assert r.status_code == 403


async def test_other_profile_fields_do_not_need_the_password(
    client: AsyncClient, test_user, auth_headers
):
    r = await client.put("/api/v1/users/me", headers=auth_headers, json={"full_name": "Renamed"})
    assert r.status_code == 200


async def test_a_guest_who_sets_credentials_becomes_a_real_account(client: AsyncClient):
    """Left flagged as a guest, the account was deleted by the retention sweep."""
    headers = await _guest(client)
    r = await client.put(
        "/api/v1/users/me",
        headers=headers,
        json={"email": "upgraded@example.com", "password": "Sup3rSecret!pw"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_guest"] is False

    login = await client.post(
        "/api/v1/users/login",
        data={"username": "upgraded@example.com", "password": "Sup3rSecret!pw"},
    )
    assert login.status_code == 200, login.text


async def test_a_guest_who_only_renames_stays_a_guest(client: AsyncClient):
    headers = await _guest(client)
    r = await client.put("/api/v1/users/me", headers=headers, json={"full_name": "Just Visiting"})
    assert r.status_code == 200
    assert r.json()["is_guest"] is True


# ── local storage ────────────────────────────────────────────────────────
@pytest.fixture
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module.settings, "LOCAL_STORAGE_PATH", str(tmp_path / "uploads"))
    return LocalStorageService()


async def test_delete_prefix_removes_only_that_directory(local_storage, tmp_path):
    await local_storage.upload_file(b"a", "videos/s1/1.mp4")
    await local_storage.upload_file(b"b", "videos/s1/2.mp4")
    await local_storage.upload_file(b"c", "videos/s2/1.mp4")

    assert await local_storage.delete_prefix("videos/s1/") == 2

    root = tmp_path / "uploads"
    assert not (root / "videos" / "s1").exists()
    assert (root / "videos" / "s2" / "1.mp4").exists()
    assert await local_storage.delete_prefix("videos/never-existed/") == 0


@pytest.mark.parametrize("prefix", ["", "/", ".", "../", "videos/../../"])
async def test_delete_prefix_refuses_to_leave_or_empty_the_storage_root(
    local_storage, tmp_path, prefix
):
    await local_storage.upload_file(b"a", "videos/s1/1.mp4")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")

    with pytest.raises(ValueError):
        await local_storage.delete_prefix(prefix)

    assert outside.exists()
    assert (tmp_path / "uploads" / "videos" / "s1" / "1.mp4").exists()


# ── media retention ──────────────────────────────────────────────────────
def test_old_clips_are_swept_and_recent_ones_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "USE_LOCAL_STORAGE", True)
    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "MEDIA_RETENTION_HOURS", 24)

    old_dir = tmp_path / "videos" / "old-session"
    new_dir = tmp_path / "videos" / "live-session"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    old_clip, new_clip = old_dir / "1.mp4", new_dir / "1.mp4"
    old_clip.write_bytes(b"x")
    new_clip.write_bytes(b"x")
    two_days_ago = time.time() - 48 * 3600
    os.utime(old_clip, (two_days_ago, two_days_ago))

    avatar = tmp_path / "avatars" / "a1" / "image.jpg"
    avatar.parent.mkdir(parents=True)
    avatar.write_bytes(b"x")
    os.utime(avatar, (two_days_ago, two_days_ago))

    assert _sweep_local_videos(time.time()) == 1
    assert not old_dir.exists(), "an emptied session directory is pruned"
    assert new_clip.exists()
    assert avatar.exists(), "the sweep must never touch avatar images"


def test_retention_of_zero_keeps_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "USE_LOCAL_STORAGE", True)
    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "MEDIA_RETENTION_HOURS", 0)
    clip = tmp_path / "videos" / "s" / "1.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"x")
    os.utime(clip, (0, 0))
    assert _sweep_local_videos(time.time()) == 0
    assert clip.exists()


# ── voice ownership ──────────────────────────────────────────────────────
async def test_a_voice_cannot_be_assigned_by_someone_who_does_not_own_it(
    client: AsyncClient, monkeypatch, db_session
):
    from app.models import Avatar

    owner, intruder = await _guest(client), await _guest(client)
    owner_id = (await client.get("/api/v1/users/me", headers=owner)).json()["id"]
    intruder_id = (await client.get("/api/v1/users/me", headers=intruder)).json()["id"]

    voice_id = "11111111-1111-4111-8111-111111111111"

    async def index():
        return [{"id": voice_id, "user_id": owner_id, "wav_path": "/voices/v.wav"}]

    monkeypatch.setattr(voices_module, "_load_index", index)

    ids = {}
    for name, uid in (("owner", owner_id), ("intruder", intruder_id)):
        avatar = Avatar(
            id=f"22222222-2222-4222-8222-22222222222{len(ids)}",
            user_id=uid,
            name=name,
            image_url="http://x/i.jpg",
            s3_key="avatars/x/image.jpg",
            status="ready",
        )
        db_session.add(avatar)
        ids[name] = avatar.id
    await db_session.commit()

    r = await client.put(
        f"/api/v1/avatars/{ids['intruder']}/voice", params={"voice_id": voice_id}, headers=intruder
    )
    assert r.status_code == 404, r.text

    r = await client.put(
        f"/api/v1/avatars/{ids['intruder']}/voice",
        params={"voice_id": "no-such-voice"},
        headers=intruder,
    )
    assert r.status_code == 404

    r = await client.put(
        f"/api/v1/avatars/{ids['owner']}/voice", params={"voice_id": voice_id}, headers=owner
    )
    assert r.status_code == 200, r.text
    assert r.json()["voice_id"] == voice_id

    # Clearing never needs a lookup.
    r = await client.put(f"/api/v1/avatars/{ids['owner']}/voice", headers=owner)
    assert r.status_code == 200 and r.json()["voice_id"] is None


# ── avatar processing ────────────────────────────────────────────────────
async def test_thumbnail_path_survives_a_dot_in_the_directory_name(tmp_path):
    """str.replace('.', '_thumb.') rewrote every dot in the path, not just the last."""
    from PIL import Image

    work = tmp_path / "tmp.v2.dir"
    work.mkdir()
    src = work / "in.png"
    Image.new("RGB", (640, 480), (120, 90, 60)).save(src)
    out = work / "out_processed.jpg"

    _, meta = await AvatarProcessor().process_image(str(src), str(out))

    thumb = Path(meta["thumbnail_path"])
    assert thumb.parent == work
    assert thumb.name == "out_processed_thumb.jpg"
    assert thumb.is_file() and out.is_file()


async def test_a_guest_cannot_become_a_permanent_account_on_a_password_alone(client: AsyncClient):
    """
    Every guest row already has a placeholder email, so "has an email" was
    always true: a password by itself made a permanent account that nobody
    could sign in to, since sign-in is by email.
    """
    headers = await _guest(client)
    r = await client.put("/api/v1/users/me", headers=headers, json={"password": "Sup3rSecret!pw"})
    assert r.status_code == 400, r.text
    assert "email" in r.json()["detail"].lower()
    me = await client.get("/api/v1/users/me", headers=headers)
    assert me.json()["is_guest"] is True


def test_reaping_a_guest_deletes_their_voice_recordings(tmp_path, monkeypatch):
    import json

    from app.celery_app import _purge_voice_profiles

    monkeypatch.chdir(tmp_path)
    voices = tmp_path / "voice_profiles"
    voices.mkdir()
    gone, kept = voices / "gone.wav", voices / "kept.wav"
    gone.write_bytes(b"voice")
    kept.write_bytes(b"voice")
    (voices / "index.json").write_text(
        json.dumps(
            [
                {"id": "v1", "user_id": "guest-1", "wav_path": str(gone)},
                {"id": "v2", "user_id": "someone-else", "wav_path": str(kept)},
            ]
        )
    )

    assert _purge_voice_profiles({"guest-1"}) == 1

    assert not gone.exists()
    assert kept.exists()
    remaining = json.loads((voices / "index.json").read_text())
    assert [e["id"] for e in remaining] == ["v2"]
    assert _purge_voice_profiles({"nobody"}) == 0
