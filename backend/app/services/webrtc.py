"""
Continuous WebRTC delivery of the avatar (MEDIA_TRANSPORT=webrtc).

The default "clips" transport sends one MP4 per sentence and the browser
swaps <video>.src between them, so every sentence boundary is a decoder
teardown: a visible stutter and an audio gap. Here the server instead holds
one peer connection per session and publishes ONE audio track and ONE video
track that never end. Between sentences they carry the avatar's still image
and silence; a generated clip is decoded here and its frames are fed into
them. Interrupting is a queue flush on this side, so the picture returns to
the idle face at once instead of whenever the client gets round to pausing.

This module knows nothing about the WebSocket manager: signalling is handed
in and out as SDP strings and state changes are reported through callbacks.
"""

from __future__ import annotations

import asyncio
import fractions
import functools
import inspect
import json
import logging
import math
import os
import time
from collections import deque
from typing import Any, Callable, Optional

from app.config import settings

logger = logging.getLogger(__name__)

try:
    import av
    import numpy as np
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.mediastreams import MediaStreamError

    WEBRTC_AVAILABLE = True
except Exception as _import_error:  # pragma: no cover - exercised only without aiortc
    # Anything, not just ImportError: aiortc pulls in native wheels (PyAV,
    # cryptography, libsrtp bindings) that fail in their own ways on a
    # mismatched platform. The transport is optional, so none of that may
    # take the app down — it simply is not offered.
    logger.info(f"WebRTC transport unavailable: {_import_error}")
    WEBRTC_AVAILABLE = False
    MediaStreamTrack = object  # type: ignore[assignment,misc]

    class MediaStreamError(Exception):  # type: ignore[no-redef]
        pass


AUDIO_RATE = 48000
# 20 ms: the Opus frame size aiortc packetises at, so one recv is one packet.
AUDIO_FRAME_SAMPLES = 960
VIDEO_CLOCK_RATE = 90000
_VIDEO_TIME_BASE = fractions.Fraction(1, VIDEO_CLOCK_RATE)
_AUDIO_TIME_BASE = fractions.Fraction(1, AUDIO_RATE)

# How much media may sit in the queue before enqueue_clip() makes its caller
# wait. A decoded 512x512 yuv420p frame is ~390 KB, so 25 fps costs ~10 MB per
# queued second, per session. The pipeline can animate faster than real time,
# and without a bound a long reply would be decoded into memory in full while
# the first sentence was still being spoken. Four seconds is enough that the
# next sentence is always decoded and waiting before the current one ends (a
# decode takes a few hundred ms at most).
#
# This is NOT the per-session memory bound. The wait happens before the
# decode and a clip is decoded whole, so what can be resident is just under
# this much PLUS one clip of up to MAX_CLIP_SECONDS: 24 s, ~235 MB at
# 512x512/25 fps in the worst case, held while that clip plays out. A typical
# sentence is 3-6 s, so the usual figure is 50-100 MB per speaking session
# and nothing for an idle one.
MAX_QUEUE_SECONDS = 4.0

# Longest single clip accepted onto the stream. A clip is decoded whole (so it
# either plays in full or falls back in full), which makes its length a memory
# cost of its own — see above. Sentences are cut at 200 characters, around
# 15 s of speech, so this sits just above the longest the live pipeline
# produces. Anything longer goes down the clips path instead of into RAM: an
# engine that renders minutes at a time (LiveAvatar), or a reply in a script
# the sentence splitter does not cut (no spaces, no ASCII punctuation).
MAX_CLIP_SECONDS = 20.0

# How long past the moment room SHOULD have appeared enqueue_clip() keeps
# waiting for it. The queue drains in real time, so the wait is known in
# advance; one that overruns by this much means nobody is pulling the tracks
# any more (a sender task that died while the peer connection still says
# "connected"), and the caller must fall back rather than wait for ever.
# Generous, because a busy event loop delivers frames late and in bursts.
ROOM_WAIT_MARGIN_SECONDS = 5.0

# With nothing queued the picture is a still, and 25 identical frames a second
# cost an encode each, for as long as the tab stays open. After
# IDLE_GRACE_SECONDS of stillness the video track sends only IDLE_FPS frames
# a second: enough to keep the browser's decoder and the RTCP machinery fed,
# a fifth of the video work. Measured on a 512x512 loopback (both peers in
# one process, so an upper bound for the server's share): an idle session
# went from 33% of a core to 19%. What remains is almost all the audio
# track — 50 Opus packets a second of silence — which is left alone: gaps in
# audio are heard, and at 1 fps of video the figure is still 16%.
# The grace period keeps the gaps between sentences at full rate, so a reply
# does not flicker between rates.
IDLE_FPS = 5
IDLE_GRACE_SECONDS = 1.0


class ClipDropped(Exception):
    """
    The stream was flushed while this clip was being prepared.

    Not a failure: the user interrupted, and the clip must not be played by
    any route — including the fallback one.
    """


def webrtc_enabled() -> bool:
    """True when this server offers the WebRTC transport to clients."""
    return WEBRTC_AVAILABLE and settings.MEDIA_TRANSPORT.strip().lower() == "webrtc"


@functools.lru_cache(maxsize=8)
def _parse_ice_servers(raw: str) -> tuple[tuple[tuple[str, Any], ...], ...]:
    """
    Parse WEBRTC_ICE_SERVERS. Cached on the raw string so a bad value is
    reported once, not on every connection.

    A malformed value yields no servers rather than an exception: host
    candidates alone still connect on a LAN or with a public IP, and a typo
    in an optional setting must not stop the app from starting.
    """
    try:
        parsed = json.loads(raw) if raw.strip() else []
        if not isinstance(parsed, list):
            raise ValueError("expected a JSON list")
        servers = []
        for entry in parsed:
            if not isinstance(entry, dict):
                raise ValueError("each entry must be an object")
            urls = entry.get("urls")
            if isinstance(urls, str):
                urls = [urls]
            if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
                raise ValueError("'urls' must be a string or a list of strings")
            server: list[tuple[str, Any]] = [("urls", tuple(urls))]
            for key in ("username", "credential"):
                value = entry.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise ValueError(f"'{key}' must be a string")
                    server.append((key, value))
            servers.append(tuple(server))
        return tuple(servers)
    except Exception as e:
        logger.error(f"WEBRTC_ICE_SERVERS is malformed ({e}); using no ICE servers")
        return ()


def ice_servers() -> list[dict]:
    """
    The configured ICE servers, in the shape the browser's RTCPeerConnection
    takes — this is what goes to the client in the `ready` frame. `username`
    and `credential` appear only when set.
    """
    out = []
    for server in _parse_ice_servers(settings.WEBRTC_ICE_SERVERS):
        entry = dict(server)
        entry["urls"] = list(entry["urls"])
        out.append(entry)
    return out


def mic_enabled() -> bool:
    """
    True when this server takes the user's microphone over the peer
    connection and decides the turns itself (see services/turn_taking.py).
    """
    if not webrtc_enabled():
        return False
    # Imported here, and only once the transport is on: this is what loads
    # Pipecat, which a server on the default transport never needs.
    from app.services.turn_taking import voice_turns_enabled

    return voice_turns_enabled()


def media_descriptor() -> dict:
    """The `media` field of the `ready` frame."""
    return {"webrtc": webrtc_enabled(), "ice_servers": ice_servers(), "mic": mic_enabled()}


# ── decoding ────────────────────────────────────────────────────────────────


def _fit_square(frame: "av.VideoFrame", size: int) -> "np.ndarray":
    """
    `frame` as packed yuv420p planes, scaled to fit size x size with its
    aspect ratio kept and the remainder black.
    """
    scale = min(size / frame.width, size / frame.height)
    # yuv420p halves the chroma planes, so both dimensions must be even.
    w = min(size, max(2, round(frame.width * scale) & ~1))
    h = min(size, max(2, round(frame.height * scale) & ~1))
    src = frame.reformat(width=w, height=h, format="yuv420p").to_ndarray().reshape(-1)
    if w == size and h == size:
        return src.reshape(size * 3 // 2, size)

    canvas = np.empty(size * size * 3 // 2, np.uint8)
    half, luma = size // 2, size * size
    y = canvas[:luma].reshape(size, size)
    u = canvas[luma : luma + half * half].reshape(half, half)
    v = canvas[luma + half * half :].reshape(half, half)
    y[:] = 16  # video-range black
    u[:] = 128
    v[:] = 128
    # Even offsets keep the chroma samples aligned with their luma.
    ox, oy = ((size - w) // 2) & ~1, ((size - h) // 2) & ~1
    hw, hh = w // 2, h // 2
    y[oy : oy + h, ox : ox + w] = src[: w * h].reshape(h, w)
    u[oy // 2 : oy // 2 + hh, ox // 2 : ox // 2 + hw] = src[w * h : w * h + hw * hh].reshape(hh, hw)
    v[oy // 2 : oy // 2 + hh, ox // 2 : ox // 2 + hw] = src[w * h + hw * hh :].reshape(hh, hw)
    return canvas.reshape(size * 3 // 2, size)


def load_idle_frame(image_path: str, size: int) -> "np.ndarray":
    """The avatar still as the frame shown whenever nothing is queued. Blocking."""
    with av.open(image_path) as container:
        for frame in container.decode(video=0):
            return _fit_square(frame, size)
    raise ValueError(f"no image data in {image_path}")


def decode_clip(
    path: str,
    size: int,
    fps: int,
    idle: "np.ndarray",
    max_seconds: float = MAX_CLIP_SECONDS,
    audio_only: bool = False,
) -> tuple[list, "np.ndarray"]:
    """
    Decode a clip into (video frames, mono s16 48 kHz samples) of EXACTLY
    equal duration. Blocking — run it in a thread.

    With `audio_only` any picture in the file is ignored and the frames are
    the idle image, as they are for a file that has no picture.

    The equal duration is the point. A muxed clip's audio and video are
    never quite the same length (AAC priming and padding, a last video frame
    with no audio under it), and on a continuous stream that difference does
    not vanish at the end of the clip the way it does in a <video> element:
    it carries into the next one. Twenty sentences each 30 ms out is more
    than half a second of lip-sync error by the end of a reply. So the video
    decides the length — n frames is n / fps seconds — and the audio is
    padded with silence or trimmed to exactly that many samples.
    """
    frames: list = []
    pcm: list = []
    pcm_samples = 0
    max_samples = int(max_seconds * AUDIO_RATE)

    with av.open(path) as container:
        # Refused from the header where the file states its length, so an
        # over-long clip costs nothing. The check inside the loop is the
        # backstop for files that do not: it only fires once max_seconds of
        # frames are already in memory.
        if container.duration is not None and container.duration > max_seconds * av.time_base:
            raise ValueError(f"clip is longer than {max_seconds:.0f}s")
        video = None if audio_only else next(iter(container.streams.video), None)
        audio = next(iter(container.streams.audio), None)
        streams = [s for s in (video, audio) if s is not None]
        if not streams:
            raise ValueError("clip has neither audio nor video")
        src_fps = float(video.average_rate or video.guessed_rate or fps) if video else float(fps)
        max_frames = int(max_seconds * src_fps)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=AUDIO_RATE)

        def take_audio(frame) -> None:
            nonlocal pcm_samples
            for out in resampler.resample(frame):
                samples = out.to_ndarray().reshape(-1).copy()
                pcm.append(samples)
                pcm_samples += len(samples)

        for packet in container.demux(*streams):
            for frame in packet.decode():
                if isinstance(frame, av.VideoFrame):
                    frames.append(_fit_square(frame, size))
                else:
                    take_audio(frame)
            if len(frames) > max_frames or pcm_samples > max_samples:
                raise ValueError(f"clip is longer than {max_seconds:.0f}s")
        if audio is not None and pcm:
            take_audio(None)  # drain what the resampler is still holding

    if frames and abs(src_fps - fps) > 0.01:
        # Nearest-earlier source frame for each output tick: duplicates
        # when the clip is slower than the stream, drops when faster.
        # Real-time length is what is preserved, not the frame count.
        n_out = max(1, round(len(frames) * fps / src_fps))
        last = len(frames) - 1
        frames = [frames[min(last, int(i * src_fps / fps))] for i in range(n_out)]
    return _equal_length(frames, pcm, fps, idle)


def _equal_length(
    frames: list, pcm: list, fps: int, idle: "np.ndarray"
) -> tuple[list, "np.ndarray"]:
    """
    The last step of every decode: `frames` and the 48 kHz chunks in `pcm`
    as (frames, samples) of exactly equal duration. Sound with no picture is
    given the idle image for as long as it runs — the same object each time,
    so it costs a list of references, not frames.
    """
    samples_per_frame = fractions.Fraction(AUDIO_RATE, fps)
    pcm_samples = sum(len(chunk) for chunk in pcm)
    if not frames:
        if not pcm_samples:
            raise ValueError("clip decoded to nothing")
        frames = [idle] * math.ceil(pcm_samples / samples_per_frame)

    target = round(len(frames) * samples_per_frame)
    out = np.zeros(target, np.int16)
    if pcm:
        joined = np.concatenate(pcm)[:target]
        out[: len(joined)] = joined
    return frames, out


def pcm_clip(
    pcm: "np.ndarray",
    sample_rate: int,
    fps: int,
    idle: "np.ndarray",
    max_seconds: float = MAX_CLIP_SECONDS,
) -> tuple[list, "np.ndarray"]:
    """
    decode_clip for sound that is already in memory: mono samples, int16 or
    float in [-1, 1], at `sample_rate`. Blocking only for the resample.
    """
    samples = np.asarray(pcm).reshape(-1)
    if sample_rate <= 0 or len(samples) > max_seconds * sample_rate:
        raise ValueError(f"clip is longer than {max_seconds:.0f}s")
    if samples.dtype != np.int16:
        samples = (np.clip(samples.astype(np.float32), -1.0, 1.0) * 32767).astype(np.int16)
    if sample_rate == AUDIO_RATE:
        return _equal_length([], [samples], fps, idle)
    frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
    frame.sample_rate = sample_rate
    resampler = av.AudioResampler(format="s16", layout="mono", rate=AUDIO_RATE)
    chunks = [
        out.to_ndarray().reshape(-1).copy()
        for step in (frame, None)  # None drains what the resampler holds back
        for out in resampler.resample(step)
    ]
    return _equal_length([], chunks, fps, idle)


# ── tracks ──────────────────────────────────────────────────────────────────


class _PacedTrack(MediaStreamTrack):  # type: ignore[misc,valid-type]
    """
    A track that hands out one frame per fixed period.

    Both of a stream's tracks tick on ONE shared epoch (the stream's), so
    frame k of either has a well-defined place on a common timeline. That is
    what lets a clip be queued so that its audio and video start on the same
    instant rather than on whichever tick each track happens to reach next.

    Pacing follows aiortc's own VideoStreamTrack: every deadline is computed
    from the epoch and the frame index, never from the previous wake-up, so
    scheduling jitter does not accumulate into drift, and the wait is a
    single sleep rather than a poll.
    """

    def __init__(self, stream: "AvatarStream", period: fractions.Fraction) -> None:
        super().__init__()
        self._stream = stream
        self._period = period
        # Index, on the shared timeline, of the next frame this track will
        # hand out. None until the first recv.
        self.position: Optional[int] = None

    async def _wait_turn(self) -> int:
        if self.readyState != "live":
            raise MediaStreamError
        now = time.monotonic()
        stream = self._stream
        if stream._epoch is None:
            stream._epoch = now
        if self.position is None:
            # Join the grid where it is now; the other track may have been
            # running for a while.
            self.position = math.ceil((now - stream._epoch) / self._period)
        wait = stream._epoch + float(self.position * self._period) - now
        if wait > 0:
            await asyncio.sleep(wait)
        if self.readyState != "live":
            raise MediaStreamError
        return self.position


class AvatarVideoTrack(_PacedTrack):
    kind = "video"

    def __init__(self, stream: "AvatarStream") -> None:
        super().__init__(stream, fractions.Fraction(1, stream.fps))
        # Consecutive grid slots that found nothing queued.
        self._idle_slots = 0
        self._idle_grace = math.ceil(IDLE_GRACE_SECONDS * stream.fps)
        # At the low idle rate only every Nth slot carries a frame.
        self._idle_step = max(1, round(stream.fps / IDLE_FPS))

    async def recv(self):
        stream = self._stream
        while True:
            index = await self._wait_turn()
            # Looked at AFTER the wait, so a flush or a clip that lands
            # during the sleep is honoured by this very slot.
            if stream._video:
                self._idle_slots = 0
                break
            self._idle_slots += 1
            if self._idle_slots <= self._idle_grace or index % self._idle_step == 0:
                break
            # Resting (see IDLE_FPS): this slot carries no frame. The track
            # still walks the grid one slot at a time rather than sleeping
            # straight to the next frame it owes, for two reasons: `position`
            # stays true, which is what _append lines a clip's audio up
            # against; and a clip queued meanwhile starts on the very next
            # slot instead of up to an idle interval late. A timer wake-up
            # is free next to the encode it replaces.
            self.position = index + 1
        pixels = stream._take_video()
        self.position = index + 1
        # A new VideoFrame per recv, even for the idle image. aiortc encodes
        # in an executor thread and its VP8 encoder writes to the frame it is
        # given (pict_type, on a forced keyframe), so one shared frame object
        # would be mutated from two threads and would carry a stale keyframe
        # flag. The copy is ~0.4 MB — well under a millisecond.
        frame = av.VideoFrame.from_ndarray(pixels, format="yuv420p")
        # From the slot index, so a skipped slot is a gap in the timestamps
        # and never a shift of the grid: the frames that are sent sit exactly
        # where they would have at full rate.
        frame.pts = round(index * self._period * VIDEO_CLOCK_RATE)
        frame.time_base = _VIDEO_TIME_BASE
        return frame


class AvatarAudioTrack(_PacedTrack):
    kind = "audio"

    def __init__(self, stream: "AvatarStream") -> None:
        super().__init__(stream, fractions.Fraction(AUDIO_FRAME_SAMPLES, AUDIO_RATE))

    async def recv(self):
        index = await self._wait_turn()
        samples = self._stream._take_audio()
        self.position = index + 1
        frame = av.AudioFrame.from_ndarray(
            samples.reshape(1, AUDIO_FRAME_SAMPLES), format="s16", layout="mono"
        )
        frame.sample_rate = AUDIO_RATE
        frame.pts = index * AUDIO_FRAME_SAMPLES
        frame.time_base = _AUDIO_TIME_BASE
        return frame


# ── the stream ──────────────────────────────────────────────────────────────


class _QueuedClip:
    """
    One clip's place in the queues: where its last frame and its last sample
    sit, counted in frames/samples pushed since the queues were last emptied.
    It has played when both tracks have taken that many.

    A clip being built up batch by batch (see ProgressiveClip) is `open`: its
    end keeps moving, so it has not played however much of it has been taken,
    and its token is not the stream's to account for yet. `handle` is that
    builder, while it is open.
    """

    __slots__ = ("token", "video_end", "audio_end", "open", "handle")

    def __init__(self, token: Any, video_end: int, audio_end: int) -> None:
        self.token = token
        self.video_end = video_end
        self.audio_end = audio_end
        self.open = False
        self.handle: Optional["ProgressiveClip"] = None


def bgr_frames_to_stream(batch: "np.ndarray", size: int) -> list:
    """
    A batch of (n, H, W, 3) uint8 BGR pictures as stream frames — the same
    fit-to-square yuv420p frames decode_clip produces. Blocking.
    """
    return [
        _fit_square(av.VideoFrame.from_ndarray(np.ascontiguousarray(picture), format="bgr24"), size)
        for picture in batch
    ]


class ProgressiveClip:
    """
    One sentence going onto the stream while its picture is still being made.

    The sound is whole from the start; the frames arrive in batches. Each
    batch is queued with exactly the stretch of sound that belongs under it,
    so however the batches are timed, a frame and its samples sit at the same
    point of the stream's timeline.

    Until finish() the clip is the caller's: retire() does not hand it back
    and a flush does not release its token. After finish() it is an ordinary
    queued clip. If the caller gives up it calls abort(), and the token never
    becomes the stream's at all.

    With `buffer_all` nothing is queued until finish(): the frames are only
    collected. That is for an engine that cannot keep up with playback — a
    sentence that starts and then stalls mid-word is worse than one that
    starts a moment later.
    """

    def __init__(
        self,
        stream: "AvatarStream",
        samples: "np.ndarray",
        expected_frames: int,
        token: Any,
        buffer_all: bool,
    ) -> None:
        self._stream = stream
        self._samples = samples
        self._cursor = 0
        self._generation = stream._generation
        self._record: Optional[_QueuedClip] = None
        self._held: list = []
        self.token = token
        self.buffer_all = buffer_all
        self.expected_frames = expected_frames
        self.duration_ms = round(expected_frames * 1000 / stream.fps)
        # Frames received from the engine, and whether any of this clip has
        # actually been put on the stream (and so may have been heard).
        self.frames_pushed = 0
        self.appended = False
        # Times the queue ran dry before the engine had delivered the frames
        # this sentence needs: each one is a stall the listener heard.
        self.underruns = 0
        self.done = False

    async def push(self, frames_bgr: "np.ndarray") -> None:
        """
        Queue the next batch of frames, (n, H, W, 3) uint8 BGR. Raises what
        enqueue_clip raises: ClipDropped if the stream was flushed since the
        clip began, RuntimeError if the stream is finished.
        """
        stream = self._stream
        if self.done:
            raise RuntimeError("clip is already finished")
        stream._check_current(self._generation)
        frames = await asyncio.to_thread(bgr_frames_to_stream, frames_bgr, stream.size)
        if not frames:
            return
        self.frames_pushed += len(frames)
        if self.buffer_all:
            stream._check_current(self._generation)
            self._held.extend(frames)
            return
        await stream._wait_for_room(self._generation)
        # From here to the end of _append there is no await.
        self._queue(frames)

    def _queue(self, frames: list) -> None:
        stream = self._stream
        # A little more than the batch can need; _append takes exactly what
        # fits under these frames on the timeline and says how much that was.
        reach = math.ceil(len(frames) * stream._samples_per_frame) + 2
        piece = self._samples[self._cursor : self._cursor + reach]
        if self._record is None:
            record = _QueuedClip(self.token, stream._video_in, stream._audio_in)
            record.open = True
            record.handle = self
            stream._clips.append(record)
            self._record = record
        self._cursor += stream._append(frames, piece, clip=self._record)
        self.appended = True

    async def finish(self) -> None:
        """
        No more frames are coming. Queues whatever sound is left (under the
        idle picture) and hands the clip over to the stream: from here on its
        token is accounted for like any other clip's.
        """
        stream = self._stream
        if self.done:
            return
        stream._check_current(self._generation)
        if self.buffer_all and self._held:
            await stream._wait_for_room(self._generation)
            frames, self._held = self._held, []
            self._queue(frames)
        stream._check_current(self._generation)
        left = self._samples[self._cursor :]
        if len(left):
            # Sound with no frame under it: the idle picture for as long as
            # it runs, as for a clip that is sound only.
            tail = [stream._idle] * math.ceil(len(left) / stream._samples_per_frame)
            self._queue(tail)
        self.done = True
        if self._record is not None:
            self._record.open = False
            self._record.handle = None
            stream._settle()

    def abort(self) -> None:
        """
        Give up on the clip. Nothing more is queued; what already was plays
        out (or is flushed by whoever is aborting). The token stays the
        caller's — the stream will neither release it nor hand it back.
        """
        if self.done:
            return
        self.done = True
        self._held = []
        if self._record is not None:
            self._record.open = False
            self._record.handle = None
            self._record.token = _NO_TOKEN
            self._stream._settle()


# The token of a clip the stream is not accounting for (an aborted
# progressive clip): never released, never handed back.
_NO_TOKEN = object()


class AvatarStream:
    """
    One session's peer connection and the two never-ending tracks on it.

    Everything that touches the queues runs on the event loop and none of it
    awaits while doing so, which is the lock: a clip's two halves are appended
    in one uninterrupted step, and a flush cannot land between them. The only
    work done off the loop is decoding, which touches no shared state.

    A clip is queued with a `token` of the caller's choosing, and the stream
    accounts for every token exactly once:
      - on_clip_released(token) when the clip has played to its last frame
        and sample, or was flushed — its source is no longer needed;
      - or in the list retire() returns, when the stream went down first —
        those clips were NOT played and are the caller's to deliver some
        other way.
    That is what lets speech put on the stream be recovered when the peer
    connection dies under it.

    Callbacks (assign after construction; any may return an awaitable, which
    is scheduled rather than awaited so a track's recv never blocks):
      on_state(state)         — the peer connection's connectionState changed,
                                or "failed" when the stream itself found it
                                is no longer being sent
      on_speaking(active)     — queued speech started (True) / ran out (False)
      on_clip_released(token) — see above
      on_remote_audio(track)  — the browser's microphone track, when
                                `accept_mic` is set and the offer's audio is
                                sendrecv. Fired during handle_offer. The
                                owner must read it for as long as the stream
                                lives: aiortc queues what arrives without
                                bound.

    The stream only ever SENDS unless `accept_mic` is set before the offer is
    handled. Media the browser offers to send is otherwise refused in the
    answer (a=sendonly), which also stops aiortc from decoding it.
    """

    def __init__(
        self,
        idle_frame: "np.ndarray",
        *,
        size: int,
        fps: int,
        max_queue_seconds: float = MAX_QUEUE_SECONDS,
    ) -> None:
        if not WEBRTC_AVAILABLE:
            raise RuntimeError("aiortc is not installed")
        self.size = size
        self.fps = fps
        self.on_state: Optional[Callable[[str], Any]] = None
        self.on_speaking: Optional[Callable[[bool], Any]] = None
        self.on_clip_released: Optional[Callable[[Any], Any]] = None
        self.on_remote_audio: Optional[Callable[[Any], Any]] = None
        # Whether an offered microphone is taken. Off unless the owner asks.
        self.accept_mic = False
        self._remote_audio: Any = None

        self._idle = idle_frame
        self._silence = np.zeros(AUDIO_FRAME_SAMPLES, np.int16)
        self._samples_per_frame = fractions.Fraction(AUDIO_RATE, fps)
        self._max_queue_seconds = max_queue_seconds

        self._video: deque = deque()
        self._audio: deque = deque()
        self._audio_samples = 0
        # Frames/samples pushed onto and taken off the queues since they were
        # last emptied; a _QueuedClip is measured against these.
        self._video_in = self._video_out = 0
        self._audio_in = self._audio_out = 0
        self._clips: deque[_QueuedClip] = deque()
        self._speaking = False
        # Bumped by every flush. A clip that began before the bump belongs to
        # a turn that has since been interrupted and must not be queued.
        self._generation = 0
        self._room = asyncio.Event()
        self._room.set()
        # Futures of wait_drained() callers, resolved when no clip is left.
        self._drain_waiters: set = set()
        self._epoch: Optional[float] = None
        # Three ways of being finished, in the order they happen:
        #   _failed  — the stream noticed it is not being sent any more. It
        #              takes no new clips but still holds the unplayed ones
        #              for the owner to collect.
        #   _retired — the owner collected them (retire()). Nothing is queued
        #              and nothing ever will be.
        #   _closed  — the tracks and the peer connection are released.
        self._failed = False
        self._retired = False
        self._closed = False
        self._pc: Optional["RTCPeerConnection"] = None
        self._callback_tasks: set = set()

        self.video = AvatarVideoTrack(self)
        self.audio = AvatarAudioTrack(self)
        # aiortc's sender stops the track it is reading when its own loop
        # dies on an unexpected error (an encoder exception, say) — and tells
        # nobody: the peer connection stays "connected". This is the only
        # sign of it.
        self.video.on("ended", self._on_track_ended)
        self.audio.on("ended", self._on_track_ended)

    @classmethod
    async def create(
        cls,
        image_path: str,
        *,
        size: Optional[int] = None,
        fps: Optional[int] = None,
        max_queue_seconds: float = MAX_QUEUE_SECONDS,
    ) -> "AvatarStream":
        """Build a stream whose idle picture is the image at `image_path`."""
        # Even, because yuv420p has half-size chroma planes.
        size = max(2, int(size or settings.AVATAR_RESOLUTION) & ~1)
        fps = max(1, int(fps or settings.AVATAR_FPS))
        idle = await asyncio.to_thread(load_idle_frame, image_path, size)
        return cls(idle, size=size, fps=fps, max_queue_seconds=max_queue_seconds)

    # ── state ────────────────────────────────────────────────────────────

    @property
    def _tracks_live(self) -> bool:
        return self.audio.readyState == "live" and self.video.readyState == "live"

    @property
    def connected(self) -> bool:
        """
        True while a clip put on the stream will reach the browser, as far as
        this side can tell. The peer connection's own state is not enough: it
        stays "connected" when a sender task has died and stopped its track.
        """
        return (
            not self._retired
            and not self._failed
            and self._pc is not None
            and self._pc.connectionState == "connected"
            and self._tracks_live
        )

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def queued_video_frames(self) -> int:
        return len(self._video)

    @property
    def queued_audio_samples(self) -> int:
        return self._audio_samples

    @property
    def queued_seconds(self) -> float:
        return max(len(self._video) / self.fps, self._audio_samples / AUDIO_RATE)

    @property
    def queued_clips(self) -> list:
        """Tokens of the clips not yet played to the end, oldest first."""
        return [clip.token for clip in self._clips]

    def _fire(self, callback: Optional[Callable], value) -> None:
        if callback is None:
            return
        try:
            result = callback(value)
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                self._callback_tasks.add(task)
                task.add_done_callback(self._callback_tasks.discard)
        except Exception as e:
            logger.warning(f"WebRTC stream callback failed: {e}")

    def _set_speaking(self, active: bool) -> None:
        if active != self._speaking:
            self._speaking = active
            self._fire(self.on_speaking, active)

    def _fail(self, reason: str) -> None:
        """
        The stream found out for itself that it is not being sent. Refuse
        clips from this instant — synchronously, so nothing more is queued
        behind the ones already stranded — and tell the owner, whose retire()
        collects those.
        """
        if self._failed or self._retired:
            return
        self._failed = True
        logger.warning(f"WebRTC stream failed: {reason}")
        self._room.set()  # anyone waiting for room re-checks and raises
        self._fire(self.on_state, "failed")

    def _on_track_ended(self) -> None:
        # Our own close() stops the tracks too; by then the stream is retired
        # and _fail does nothing.
        self._fail("a track stopped while the peer connection was up")

    # ── signalling ───────────────────────────────────────────────────────

    async def handle_offer(self, sdp: str) -> str:
        """
        Answer a browser's offer. Returns the complete answer SDP: aiortc
        gathers every candidate inside setLocalDescription, so there is
        nothing left to trickle.
        """
        if self._retired:
            raise RuntimeError("stream is closed")
        if self._pc is not None:
            # A renegotiation would need the old transports torn down in step
            # with the new ones; a fresh stream per offer is simpler and is
            # what the owner does.
            raise RuntimeError("stream has already been negotiated")

        servers = [RTCIceServer(**server) for server in ice_servers()]
        pc = self._pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
        pc.on("connectionstatechange", self._on_connection_state)
        # Before the offer is applied: that is when aiortc announces the
        # tracks the browser means to send.
        pc.on("track", self._on_remote_track)

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        offered = {t.kind for t in pc.getTransceivers()}
        if not {"audio", "video"} <= offered:
            # addTrack would otherwise invent a transceiver the offer never
            # asked for, and the answer would not match the offer.
            raise ValueError("offer must ask for both audio and video")
        # addTrack reuses the transceiver the offer created for that kind, so
        # the avatar's voice and the user's microphone share ONE audio m-line.
        pc.addTrack(self.audio)
        pc.addTrack(self.video)
        for transceiver in pc.getTransceivers():
            # Receive on the microphone's transceiver and nowhere else. A
            # sendrecv offer answered in kind would have aiortc decode
            # whatever the browser sent — video included — into a queue
            # nobody reads. (A recvonly offer is answered sendonly whatever
            # is set here; this changes nothing for it.)
            takes_mic = (
                self._remote_audio is not None and transceiver.receiver.track is self._remote_audio
            )
            if not takes_mic and transceiver.direction == "sendrecv":
                transceiver.direction = "sendonly"
        await pc.setLocalDescription(await pc.createAnswer())
        return pc.localDescription.sdp

    def _on_remote_track(self, track) -> None:
        # Only the first audio track, and only when the owner asked for one.
        if track.kind != "audio" or not self.accept_mic or self._remote_audio is not None:
            return
        if self._retired:
            return
        self._remote_audio = track
        self._fire(self.on_remote_audio, track)

    def _on_connection_state(self) -> None:
        if self._retired or self._pc is None:
            return
        state = self._pc.connectionState
        logger.info(f"WebRTC connection state: {state}")
        if state in ("failed", "closed", "disconnected"):
            # Here, not when the owner gets round to handling the callback
            # below (it is scheduled, not called): a clip that finishes
            # decoding in between must be refused, not queued onto a
            # connection that is already gone.
            self._failed = True
            self._room.set()
        self._fire(self.on_state, state)

    # ── media ────────────────────────────────────────────────────────────

    def _check_current(self, generation: int) -> None:
        # Order matters. A stream that is finished must raise the plain error
        # (the caller falls back to a clip) even though retiring also empties
        # the queues; only a live stream that was flushed means "the user
        # interrupted, play this nowhere".
        if self._retired:
            raise RuntimeError("stream is closed")
        if self._failed or not self._tracks_live:
            raise RuntimeError("stream is no longer being sent")
        if generation != self._generation:
            raise ClipDropped()

    async def enqueue_clip(self, path: str, token: Any = None) -> int:
        """
        Put a clip on the stream, after whatever is already queued. Returns
        its duration in milliseconds. From then on `token` is accounted for
        through on_clip_released or retire() — see the class docstring.

        Waits (cancellably) while more than the cap is queued. Raises
        ClipDropped if the stream was flushed in the meantime; RuntimeError
        if the stream is finished or its queue has stopped draining; and
        whatever the decoder raised if the file cannot be read. In every one
        of those cases nothing of the clip was queued.
        """
        return await self._enqueue(
            functools.partial(decode_clip, path, self.size, self.fps, self._idle), token
        )

    async def enqueue_audio(
        self, source: Any, token: Any = None, *, sample_rate: int = AUDIO_RATE
    ) -> int:
        """
        Put sound on the stream with the idle picture over it: what a clip
        from the still-image engine amounts to, without the clip. `source` is
        an audio file's path, or mono samples in memory at `sample_rate`
        (int16, or float in [-1, 1]).

        Everything else is enqueue_clip: the same waiting, errors, return
        value and accounting for `token`, and it shares one timeline with the
        clips around it. Its length is rounded up to a whole video frame,
        like any clip's, with silence — which is what keeps the next clip's
        picture and sound starting together.
        """
        if isinstance(source, (str, os.PathLike)):
            decode = functools.partial(
                decode_clip, os.fspath(source), self.size, self.fps, self._idle, audio_only=True
            )
        else:
            decode = functools.partial(pcm_clip, source, sample_rate, self.fps, self._idle)
        return await self._enqueue(decode, token)

    async def begin_clip(
        self, audio_path: str, token: Any = None, *, buffer_all: bool = False
    ) -> ProgressiveClip:
        """
        Start a clip whose picture will arrive in batches (ProgressiveClip).
        The sentence's whole sound is decoded here, up front. Raises what
        enqueue_clip raises; nothing is queued yet.
        """
        self._check_current(self._generation)
        generation = self._generation
        frames, samples = await asyncio.to_thread(
            decode_clip, os.fspath(audio_path), self.size, self.fps, self._idle, audio_only=True
        )
        self._check_current(generation)
        clip = ProgressiveClip(self, samples, len(frames), token, buffer_all)
        clip._generation = generation
        return clip

    async def _enqueue(self, decode: Callable[[], tuple], token: Any) -> int:
        """Wait for room, run `decode` in a thread, queue what it returns."""
        generation = self._generation
        await self._wait_for_room(generation)

        frames, samples = await asyncio.to_thread(decode)
        # Re-checked here, after the thread: a flush or a failure during the
        # decode must win. From this line to the end of _append there is no
        # await.
        self._check_current(generation)
        self._append(frames, samples, token)
        return round(len(frames) * 1000 / self.fps)

    async def _wait_for_room(self, generation: int) -> None:
        """Wait, cancellably, until less than the cap is queued."""
        while True:
            self._check_current(generation)
            excess = self.queued_seconds - self._max_queue_seconds
            if excess < 0:
                break
            self._room.clear()
            # The queue empties in real time, so room is `excess` seconds
            # away — if anyone is still pulling the tracks. A timer rather
            # than asyncio.wait_for: on Python 3.11 wait_for can swallow a
            # cancellation that arrives just as the wait completes, and this
            # is exactly where a turn is cancelled from (a flush makes room
            # and the interrupt cancels the turn in the same breath).
            timer = asyncio.get_running_loop().call_later(
                excess + ROOM_WAIT_MARGIN_SECONDS, self._fail, "its queue is not draining"
            )
            try:
                await self._room.wait()
            finally:
                timer.cancel()

    def _append(
        self,
        frames: list,
        samples: "np.ndarray",
        token: Any = None,
        *,
        clip: Optional[_QueuedClip] = None,
    ) -> int:
        """
        Queue `frames` with `samples` under them. With `clip`, they extend
        that (open) clip instead of being a clip of their own. Returns how
        many of `samples` were used.
        """
        video, audio = self.video, self.audio
        per_frame = self._samples_per_frame
        if video.position is not None and audio.position is not None:
            # Where each queue currently ends on the shared timeline, in
            # audio samples. After a pause these differ by up to a frame —
            # the tracks tick at different rates and reach "now" separately.
            # Level them first, so the clip's first sample sits under its
            # first frame.
            video_end = (video.position + len(self._video)) * per_frame
            audio_end = audio.position * AUDIO_FRAME_SAMPLES + self._audio_samples
            # Under one sample is not an offset: it is the rounding left by a
            # frame rate that does not divide 48000 (see below), which is all
            # the two ever differ by mid-burst.
            if audio_end - video_end >= 1:
                lead = math.ceil((audio_end - video_end) / per_frame)
                self._video.extend([self._idle] * lead)
                self._video_in += lead
                video_end += lead * per_frame
            gap = round(video_end - audio_end)
            if gap > 0:
                self._push_audio(np.zeros(gap, np.int16))
                audio_end += gap
        else:
            # Not being pulled yet: both queues start from the same instant.
            video_end = len(self._video) * per_frame
            audio_end = self._audio_samples

        # The clip's audio must end where its video ends ON THE TIMELINE, not
        # merely be the right length by itself. At 25 fps the two are the same
        # thing (1920 samples a frame). At 18 fps a frame is 2666.67 samples,
        # a clip's own length rounds to a whole number, and those roundings
        # used to pile up until the levelling above mistook them for an
        # offset and put a still frame and a frame of silence in the middle
        # of a sentence. Fitting each clip to the cumulative position keeps
        # the remainder under one sample for ever.
        want = max(0, round(video_end + len(frames) * per_frame) - audio_end)
        used = min(want, len(samples))
        if want < len(samples):
            samples = samples[:want]
        elif want > len(samples):
            samples = np.concatenate([samples, np.zeros(want - len(samples), np.int16)])

        self._video.extend(frames)
        self._video_in += len(frames)
        self._push_audio(samples)
        if clip is None:
            self._clips.append(_QueuedClip(token, self._video_in, self._audio_in))
        else:
            clip.video_end, clip.audio_end = self._video_in, self._audio_in
        self._set_speaking(True)
        return used

    def _push_audio(self, samples: "np.ndarray") -> None:
        if len(samples):
            self._audio.append(samples)
            self._audio_samples += len(samples)
            self._audio_in += len(samples)

    def _take_video(self) -> "np.ndarray":
        if not self._video:
            return self._idle
        pixels = self._video.popleft()
        self._video_out += 1
        self._after_take()
        return pixels

    def _take_audio(self) -> "np.ndarray":
        if not self._audio_samples:
            return self._silence
        out = np.zeros(AUDIO_FRAME_SAMPLES, np.int16)
        filled = 0
        while filled < AUDIO_FRAME_SAMPLES and self._audio:
            head = self._audio[0]
            take = min(AUDIO_FRAME_SAMPLES - filled, len(head))
            out[filled : filled + take] = head[:take]
            if take == len(head):
                self._audio.popleft()
            else:
                self._audio[0] = head[take:]
            filled += take
        self._audio_samples -= filled
        self._audio_out += filled
        self._after_take()
        return out

    def _after_take(self) -> None:
        if self.queued_seconds < self._max_queue_seconds:
            self._room.set()
        if not self._video and not self._audio_samples:
            # Ran dry. If a clip is still being built and the engine owes it
            # frames, the listener just heard it stall.
            for clip in self._clips:
                handle = clip.handle
                if clip.open and handle is not None and handle.appended:
                    if handle.frames_pushed < handle.expected_frames:
                        handle.underruns += 1
        self._settle()

    def _settle(self) -> None:
        """Release what has played, and notice when speech has run out."""
        # "Played" means handed to the sender — the last few frames are still
        # in the encoder and the browser's jitter buffer. That is as close as
        # this side can know. An open clip has not played, whatever has been
        # taken: more of it is coming.
        while self._clips:
            head = self._clips[0]
            if head.open:
                break
            if self._video_out < head.video_end or self._audio_out < head.audio_end:
                break
            self._clips.popleft()
            if head.token is not _NO_TOKEN:
                self._fire(self.on_clip_released, head.token)
        # A clip still being built is speech in progress even while the queue
        # is momentarily empty between two of its batches.
        building = any(clip.open for clip in self._clips)
        if self._speaking and not building and not self._video and not self._audio_samples:
            self._set_speaking(False)
        if not self._clips:
            self._notify_drained()

    def _notify_drained(self) -> None:
        for waiter in self._drain_waiters:
            if not waiter.done():
                waiter.set_result(True)

    def _empty_queues(self) -> list:
        """
        Drop everything queued. Returns the tokens of the clips dropped —
        those the stream was accounting for. A clip still being built is its
        builder's (who finds out at its next push), and an aborted one is
        nobody's.
        """
        tokens = [
            clip.token for clip in self._clips if not clip.open and clip.token is not _NO_TOKEN
        ]
        for clip in self._clips:
            clip.open = False
            clip.handle = None
        self._clips.clear()
        self._video.clear()
        self._audio.clear()
        self._audio_samples = 0
        self._video_in = self._video_out = 0
        self._audio_in = self._audio_out = 0
        self._room.set()  # anyone waiting for room re-checks its generation
        self._notify_drained()
        return tokens

    async def wait_drained(self, margin: float = 1.0) -> bool:
        """
        Wait until every queued clip has played (or been flushed, or the
        stream retired). Returns False if that took longer than the queue is
        long, i.e. the stream is not draining.

        For a caller about to deliver the NEXT sentence by another route: the
        browser cannot delay a live stream, so whatever is sent beside it
        plays over it.
        """
        if not self._clips:
            return True
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()

        def give_up() -> None:
            if not waiter.done():
                waiter.set_result(False)

        self._drain_waiters.add(waiter)
        # A timer, not asyncio.wait_for, for the reason given in enqueue_clip.
        timer = loop.call_later(self.queued_seconds + margin, give_up)
        try:
            return await waiter
        finally:
            timer.cancel()
            self._drain_waiters.discard(waiter)

    def flush(self) -> bool:
        """
        Drop everything queued, now (barge-in). The next frame out of each
        track is idle. Returns True if speech was actually cut off.

        The dropped clips are released, not kept for recovery: the user
        interrupted them.
        """
        self._generation += 1
        dropped = self._empty_queues()
        was_speaking = self._speaking
        self._set_speaking(False)
        for token in dropped:
            self._fire(self.on_clip_released, token)
        return was_speaking

    def retire(self) -> list:
        """
        The stream is going down: stop it taking clips and hand back the ones
        it did not finish, oldest first — including the one part-way through,
        whole. Synchronous, so that between a caller deciding the stream is
        finished and the stream refusing the next clip there is no await for
        a decode to complete in; and final, so a clip handed back here can
        never also be played from the queue or handed back twice.

        The callbacks are dropped: the owner asked for this and does not need
        telling, and by now the session they point at may belong to a newer
        connection.
        """
        if self._retired:
            return []
        self._retired = True
        self.on_state = self.on_speaking = self.on_clip_released = None
        self.on_remote_audio = None
        self._generation += 1
        self._speaking = False
        return [token for token in self._empty_queues() if token is not None]

    async def close(self) -> None:
        """
        Release everything. Idempotent, and never raises. Clips still queued
        are discarded — call retire() first to get them back.
        """
        if self._closed:
            return
        self._closed = True
        self.retire()
        for track in (self.audio, self.video):
            try:
                track.stop()
            except Exception:
                pass
        if self._pc is not None:
            try:
                await self._pc.close()
            except Exception as e:
                logger.debug(f"Peer connection close failed: {e}")
