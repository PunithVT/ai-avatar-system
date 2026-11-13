"""
Persistent MuseTalk worker.

Lifecycle:
  1. Start: reads one JSON line {
       "unet_model_path": ...,
       "unet_config": ...,
       "whisper_dir": ...,
       "vae_type": ...,
       "use_float16": true/false   <- optional, default false (enable on GPU for ~2x speedup)
     }
  2. Loads all models once, writes "READY\\n" on the protocol channel.
  3. Loop: reads one JSON job line {
           "id": ..., "image": ..., "audio": ...,
           "output": ..., "coord_cache": ...,
           "cancel_file": ...,   <- if this path appears, drop the job
           "frames_dir": ...}    <- optional; see "Frames as they are made"
           runs inference, writes {"id":...,"status":"ok","output":...}\\n
           or {"id":...,"status":"error","msg":...}\\n

Frames as they are made
A job that names a "frames_dir" also gets its composited frames one UNet
batch at a time, before the video file exists: each batch is written there as
<seq:06d>.bgr (raw uint8, BGR, shape (count, height, width, 3)) and announced
with {"id":...,"status":"frames","seq":k,"count":n,"height":H,"width":W,
"path":...}\\n. The file is complete when the line is sent (it is written
under another name and renamed). The MP4 and the final "ok" follow exactly as
without it. A job with no "frames_dir" writes and says nothing extra.

All paths may be absolute or relative to cwd (the MuseTalk repo root).

THE PROTOCOL CHANNEL IS NOT sys.stdout
The parent reads this process's original stdout as a line protocol. Upstream
MuseTalk prints to stdout freely — load_all_model() announces the UNet path
before any model is loaded, preprocessing prints "reading images..." on every
job — and each of those lines landed in the protocol stream. The parent took
the first one for the READY line, decided the worker had failed to start, and
killed it; lip-sync fell back to the static image on every sentence.

So before anything else is imported, the original stdout is duplicated onto a
private handle that only _reply() writes to, and fd 1 itself is pointed at
stderr. Every print() in every library — and anything a child process such as
ffmpeg writes — now goes to the stderr log, and cannot reach the parent.
"""

import os
import sys

if __name__ == "__main__":
    _PROTO = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
else:
    # Imported rather than run (the test suite loads the pure helpers this
    # way): leave the importing process's streams alone.
    _PROTO = sys.stdout

import json  # noqa: E402
import pickle  # noqa: E402
import subprocess  # noqa: E402
import traceback  # noqa: E402
from collections import OrderedDict  # noqa: E402
from typing import Any, List  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402
from musetalk.utils.audio_processor import AudioProcessor  # noqa: E402
from musetalk.utils.blending import get_image  # noqa: E402
from musetalk.utils.face_parsing import FaceParsing  # noqa: E402
from musetalk.utils.preprocessing import (  # noqa: E402
    coord_placeholder,
    get_landmark_and_bbox,
    read_imgs,
)
from musetalk.utils.utils import datagen, load_all_model  # noqa: E402
from transformers import WhisperModel  # noqa: E402

FPS = 25
EXTRA_MARGIN = 10
BATCH_SIZE = 8
AUDIO_PAD_L = 2
AUDIO_PAD_R = 2


# Prepared avatars kept in memory: (coords, frames, VAE latents) per image.
# Face detection and the VAE encode depend only on the avatar image, which is
# the same for every sentence of a conversation, so redoing them per job was
# pure overhead on the path that decides time-to-first-frame.
_AVATAR_CACHE_SIZE = 8
_avatar_cache: "OrderedDict[tuple, tuple]" = OrderedDict()


class JobCancelled(Exception):
    """The parent abandoned this job; stop working on it."""


def _check_cancelled(job) -> None:
    """
    Stop if the parent has asked for this job to be dropped.

    The signal is a file, because this process is single-threaded and busy:
    it cannot read another line from stdin until the current job returns.
    Without a way to stop, a job whose caller had gone (the user spoke over
    the avatar) ran to completion anyway, and the reply the user was now
    actually waiting for queued behind work nobody would ever watch.
    """
    cancel_file = job.get("cancel_file")
    if cancel_file and os.path.exists(cancel_file):
        raise JobCancelled()


def _reply(obj: dict):
    _PROTO.write(json.dumps(obj) + "\n")
    _PROTO.flush()


def _encode_video(frames, audio_path: str, output_path: str, fps: int = FPS) -> None:
    """
    Encode BGR frames + the audio track into an MP4 in ONE ffmpeg pass.

    Previously every frame was written to disk as a PNG, encoded to H.264,
    and then that file was decoded and encoded a second time by the mux step
    (which also re-ran with default quality, discarding the crf chosen for the
    first pass). Frames are now piped raw into a single encode.

    argv list, not a shell string: a path containing a space used to split
    into two arguments, and os.system() ignored the exit code — a failed mux
    still reported "ok" with no output file.

    +faststart puts the index at the front so a browser can begin playback
    before the whole file has arrived.
    """
    if not frames:
        raise RuntimeError("No frames to encode")
    h, w = frames[0].shape[:2]
    # libx264 with yuv420p needs even dimensions.
    pad = "pad=ceil(iw/2)*2:ceil(ih/2)*2"
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
        "-i", audio_path,
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", f"{pad},format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]  # fmt: skip
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=sys.stderr
    )
    try:
        for frame in frames:
            if frame.shape[:2] != (h, w):
                frame = cv2.resize(frame, (w, h))
            proc.stdin.write(np.ascontiguousarray(frame, dtype=np.uint8).tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass  # ffmpeg died early; the return code below reports it
    rc = proc.wait()
    if rc != 0 or not os.path.isfile(output_path):
        raise RuntimeError(f"ffmpeg failed to encode the video (exit {rc})")


def _prepare_avatar(image_path: str, coord_cache, vae):
    """
    Face box, source frame and VAE latents for an avatar image, cached.

    Keyed on path + mtime + size so replacing the image invalidates the entry.
    """
    try:
        st = os.stat(image_path)
        key = (image_path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (image_path, 0, 0)
    hit = _avatar_cache.get(key)
    if hit is not None:
        _avatar_cache.move_to_end(key)
        return hit

    input_img_list = [image_path]
    coord_list: List[Any]
    frame_list: List[Any]
    if coord_cache and os.path.exists(coord_cache):
        with open(coord_cache, "rb") as f:
            coord_list = list(pickle.load(f))
        frame_list = list(read_imgs(input_img_list))
    else:
        _coords, _frames = get_landmark_and_bbox(input_img_list, 0)
        coord_list, frame_list = list(_coords), list(_frames)
        if coord_cache:
            os.makedirs(os.path.dirname(coord_cache), exist_ok=True)
            with open(coord_cache, "wb") as f:
                pickle.dump(coord_list, f)

    if not frame_list or all(c == coord_placeholder for c in coord_list):
        raise RuntimeError("No face detected in avatar image")

    input_latent_list = []
    for bbox, frame in zip(coord_list, frame_list):
        if bbox == coord_placeholder:
            continue
        x1, y1, x2, y2 = bbox
        y2 = min(y2 + EXTRA_MARGIN, frame.shape[0])
        crop = cv2.resize(frame[y1:y2, x1:x2], (256, 256), interpolation=cv2.INTER_LANCZOS4)
        input_latent_list.append(vae.get_latents_for_unet(crop))  # type: ignore[arg-type]

    if not input_latent_list:
        raise RuntimeError("No valid face crops produced")

    prepared = (coord_list, frame_list, input_latent_list)
    _avatar_cache[key] = prepared
    while len(_avatar_cache) > _AVATAR_CACHE_SIZE:
        _avatar_cache.popitem(last=False)
    return prepared


def _load_face_restorer(mode: str, model_path: str, device):
    """
    Load GFPGAN once, or return None.

    Never raises. Restoration is a quality nicety; a missing weight file or a
    dependency that moved must degrade to plain MuseTalk output rather than
    take down lip-sync, which is the actual feature. Failures are reported on
    stderr (which the parent captures to worker_stderr.log) and then dropped.
    """
    if mode != "gfpgan":
        return None
    if device.type != "cuda":
        # On CPU a MuseTalk turn already takes 30-90 s; adding a per-frame
        # restoration pass would make it unusable rather than nicer.
        sys.stderr.write("INFO: face restoration skipped — requires CUDA\n")
        sys.stderr.flush()
        return None
    if not os.path.isfile(model_path):
        sys.stderr.write(
            f"WARN: face restoration disabled — weights not found at {model_path}. "
            f"Run scripts/setup_face_restore.sh\n"
        )
        sys.stderr.flush()
        return None
    try:
        from gfpgan import GFPGANer

        restorer = GFPGANer(
            model_path=model_path,
            upscale=1,  # compositing is MuseTalk's job; only restore detail
            arch="clean",
            channel_multiplier=2,
            bg_upsampler=None,  # background is untouched original pixels already
        )
        sys.stderr.write("INFO: face restoration enabled (gfpgan)\n")
        sys.stderr.flush()
        return restorer
    except Exception as e:
        sys.stderr.write(f"WARN: face restoration disabled — {type(e).__name__}: {e}\n")
        sys.stderr.flush()
        return None


def _restore(restorer, frame):
    """
    Restore the face in a composited frame, or return it unchanged.

    Applied after MuseTalk has blended its generated mouth back in, so GFPGAN
    sees a complete face to detect. Running it on the raw generated region
    instead would be cheaper, but that region is a jaw/mouth crop and the face
    detector frequently finds nothing in it.

    only_center_face keeps it to the avatar rather than anyone in the
    background; paste_back composites the restored face into the frame.
    """
    if restorer is None:
        return frame
    try:
        _, _, restored = restorer.enhance(
            frame, has_aligned=False, only_center_face=True, paste_back=True
        )
        return restored if restored is not None else frame
    except Exception as e:
        # Per-frame failure must not abort the turn; one bad frame is invisible,
        # a failed turn is not.
        sys.stderr.write(f"WARN: face restoration failed on a frame: {e}\n")
        sys.stderr.flush()
        return frame


@torch.no_grad()
def _run_job(job, vae, unet, pe, audio_processor, whisper, fp, timesteps, device, restorer=None):
    # no_grad: this is pure inference. Without it autograd recorded the whole
    # UNet + VAE-decode graph for every batch — memory and time spent on
    # gradients nothing ever asked for.
    image_path = job["image"]
    audio_path = job["audio"]
    output_path = job["output"]

    _check_cancelled(job)
    coord_list, frame_list, input_latent_list = _prepare_avatar(
        image_path, job.get("coord_cache"), vae
    )

    # ── audio features ───────────────────────────────────────────────────────
    weight_dtype = unet.model.dtype
    whisper_input_features, librosa_length = audio_processor.get_audio_feature(audio_path)
    whisper_chunks = audio_processor.get_whisper_chunk(
        whisper_input_features,
        device,
        weight_dtype,
        whisper,
        librosa_length,
        fps=FPS,
        audio_padding_length_left=AUDIO_PAD_L,
        audio_padding_length_right=AUDIO_PAD_R,
    )

    frame_list_cycle: List[Any] = frame_list + list(reversed(frame_list))  # type: ignore[operator]
    coord_list_cycle: List[Any] = coord_list + list(reversed(coord_list))  # type: ignore[operator]
    latent_list_cycle: List[Any] = input_latent_list + list(reversed(input_latent_list))

    # ── UNet inference, blended batch by batch ───────────────────────────────
    gen = datagen(
        whisper_chunks=whisper_chunks,
        vae_encode_latents=latent_list_cycle,
        batch_size=BATCH_SIZE,
        delay_frame=0,
        device=device,
    )
    frames_dir = job.get("frames_dir")
    if frames_dir:
        os.makedirs(frames_dir, exist_ok=True)

    # Each batch is composited as soon as it is generated, not after the
    # whole sentence has been: the frames are the same and in the same order,
    # but the first of them exist after one batch instead of after all of
    # them — which is what lets a caller start showing the sentence early.
    out_frames = []
    generated = 0  # index into the avatar cycle; counts skipped frames too
    seq = 0
    for whisper_batch, latent_batch in gen:
        _check_cancelled(job)
        audio_feat = pe(whisper_batch)
        latent_batch = latent_batch.to(dtype=weight_dtype)
        pred = unet.model(latent_batch, timesteps, encoder_hidden_states=audio_feat).sample
        batch_frames = []
        for res_frame in vae.decode_latents(pred):
            i = generated
            generated += 1
            bbox = coord_list_cycle[i % len(coord_list_cycle)]
            # .copy(), not deepcopy: an ndarray copy is all that is needed and
            # the cached source frame must not be drawn on.
            ori = frame_list_cycle[i % len(frame_list_cycle)].copy()
            x1, y1, x2, y2 = bbox
            y2 = min(y2 + EXTRA_MARGIN, ori.shape[0])
            try:
                res_frame = cv2.resize(res_frame.astype(np.uint8), (x2 - x1, y2 - y1))
            except Exception:
                continue
            combined = get_image(ori, res_frame, [x1, y1, x2, y2], mode="jaw", fp=fp)
            batch_frames.append(_restore(restorer, combined))
        out_frames.extend(batch_frames)
        if frames_dir and batch_frames:
            _emit_frames(job.get("id"), frames_dir, seq, batch_frames)
            seq += 1

    # ── assemble video ───────────────────────────────────────────────────────
    _check_cancelled(job)
    _encode_video(out_frames, audio_path, output_path)


def _emit_frames(job_id, frames_dir: str, seq: int, frames) -> None:
    """
    Hand one batch of finished frames to the parent, ahead of the video file.

    Written under a temporary name and renamed, so the file is whole by the
    time its line is on the protocol channel: the parent reads it the moment
    it sees the line, and a half-written file would be a torn picture.
    """
    h, w = frames[0].shape[:2]
    same_size = [f if f.shape[:2] == (h, w) else cv2.resize(f, (w, h)) for f in frames]
    block = np.ascontiguousarray(np.stack(same_size), dtype=np.uint8)
    path = os.path.join(frames_dir, f"{seq:06d}.bgr")
    with open(path + ".tmp", "wb") as f:
        f.write(block.tobytes())
    os.replace(path + ".tmp", path)
    _reply(
        {
            "id": job_id,
            "status": "frames",
            "seq": seq,
            "count": int(block.shape[0]),
            "height": int(h),
            "width": int(w),
            "path": path,
        }
    )


def main():
    # ── read init config ──────────────────────────────────────────────────────
    init = json.loads(sys.stdin.readline())

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    use_float16 = init.get("use_float16", False)

    vae, unet, pe = load_all_model(
        unet_model_path=init["unet_model_path"],
        vae_type=init["vae_type"],
        unet_config=init["unet_config"],
        device=device,
    )

    # float16 on GPU = ~2× faster via Tensor Cores (A10G / L4 / V100 all support it)
    if use_float16 and device.type == "cuda":
        pe = pe.half()
        vae.vae = vae.vae.half()
        unet.model = unet.model.half()
        sys.stderr.write("INFO: float16 enabled — ~2× faster on GPU\n")
        sys.stderr.flush()

    pe = pe.to(device)
    vae.vae = vae.vae.to(device)
    unet.model = unet.model.to(device)

    weight_dtype = unet.model.dtype
    audio_processor = AudioProcessor(feature_extractor_path=init["whisper_dir"])
    whisper = WhisperModel.from_pretrained(init["whisper_dir"])
    whisper = whisper.to(device=device, dtype=weight_dtype).eval()
    whisper.requires_grad_(False)

    fp = FaceParsing()
    timesteps = torch.tensor([0], device=device)

    # Loaded once, like every other model here — the worker is persistent
    # precisely so per-turn cost is inference only.
    restorer = _load_face_restorer(
        init.get("face_restore", "off"), init.get("face_restore_model", ""), device
    )

    _PROTO.write("READY\n")
    _PROTO.flush()

    # ── job loop ──────────────────────────────────────────────────────────────
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            job = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # Echo the job id back. The parent may abandon a job it was waiting on
        # (the user interrupted the avatar); the result still arrives here
        # later, and without an id the parent would take it for the answer to
        # its NEXT job and stay one result behind for the life of the process.
        job_id = job.get("id")
        try:
            _run_job(job, vae, unet, pe, audio_processor, whisper, fp, timesteps, device, restorer)
            _reply({"id": job_id, "status": "ok", "output": job["output"]})
        except JobCancelled:
            _reply({"id": job_id, "status": "cancelled"})
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            _reply({"id": job_id, "status": "error", "msg": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
