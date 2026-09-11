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
  2. Loads all models once, prints "READY\\n" to stdout.
  3. Loop: reads one JSON job line {
           "image": ..., "audio": ...,
           "output": ..., "coord_cache": ...}
           runs inference, prints {"status":"ok","output":...}\\n
           or {"status":"error","msg":...}\\n

All paths may be absolute or relative to cwd (the MuseTalk repo root).
"""

import copy
import json
import os
import pickle
import shutil
import sys
import traceback
from typing import Any, List

import cv2
import numpy as np
import torch
from musetalk.utils.audio_processor import AudioProcessor
from musetalk.utils.blending import get_image
from musetalk.utils.face_parsing import FaceParsing
from musetalk.utils.preprocessing import coord_placeholder, get_landmark_and_bbox, read_imgs
from musetalk.utils.utils import datagen, load_all_model
from transformers import WhisperModel

FPS = 25
EXTRA_MARGIN = 10
BATCH_SIZE = 8
AUDIO_PAD_L = 2
AUDIO_PAD_R = 2


def _reply(obj: dict):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _run_job(job, vae, unet, pe, audio_processor, whisper, fp, timesteps, device):
    image_path = job["image"]
    audio_path = job["audio"]
    output_path = job["output"]
    coord_cache = job.get("coord_cache")

    input_img_list = [image_path]

    # ── face coordinates (cached per avatar image) ───────────────────────────
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

    # ── VAE-encode frames ────────────────────────────────────────────────────
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

    frame_list_cycle: List[Any] = frame_list + list(reversed(frame_list))  # type: ignore[operator]
    coord_list_cycle: List[Any] = coord_list + list(reversed(coord_list))  # type: ignore[operator]
    latent_list_cycle: List[Any] = input_latent_list + list(reversed(input_latent_list))

    # ── UNet inference ───────────────────────────────────────────────────────
    gen = datagen(
        whisper_chunks=whisper_chunks,
        vae_encode_latents=latent_list_cycle,
        batch_size=BATCH_SIZE,
        delay_frame=0,
        device=device,
    )
    res_frame_list = []
    for whisper_batch, latent_batch in gen:
        audio_feat = pe(whisper_batch)
        latent_batch = latent_batch.to(dtype=weight_dtype)
        pred = unet.model(latent_batch, timesteps, encoder_hidden_states=audio_feat).sample
        for f in vae.decode_latents(pred):
            res_frame_list.append(f)

    # ── blend back ───────────────────────────────────────────────────────────
    frames_dir = output_path + "_frames"
    os.makedirs(frames_dir, exist_ok=True)
    for i, res_frame in enumerate(res_frame_list):
        bbox = coord_list_cycle[i % len(coord_list_cycle)]
        ori = copy.deepcopy(frame_list_cycle[i % len(frame_list_cycle)])
        x1, y1, x2, y2 = bbox
        y2 = min(y2 + EXTRA_MARGIN, ori.shape[0])
        try:
            res_frame = cv2.resize(res_frame.astype(np.uint8), (x2 - x1, y2 - y1))
        except Exception:
            continue
        combined = get_image(ori, res_frame, [x1, y1, x2, y2], mode="jaw", fp=fp)
        cv2.imwrite(f"{frames_dir}/{str(i).zfill(8)}.png", combined)

    # ── assemble video ───────────────────────────────────────────────────────
    tmp_vid = output_path + ".tmp.mp4"
    os.system(
        f"ffmpeg -y -v warning -r {FPS} -f image2 "
        f"-i {frames_dir}/%08d.png "
        f"-vcodec libx264 -vf format=yuv420p -crf 18 {tmp_vid}"
    )
    os.system(f"ffmpeg -y -v warning -i {audio_path} -i {tmp_vid} {output_path}")
    shutil.rmtree(frames_dir)
    os.remove(tmp_vid)


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

    sys.stdout.write("READY\n")
    sys.stdout.flush()

    # ── job loop ──────────────────────────────────────────────────────────────
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            job = json.loads(raw)
        except json.JSONDecodeError:
            continue
        try:
            _run_job(job, vae, unet, pe, audio_processor, whisper, fp, timesteps, device)
            _reply({"status": "ok", "output": job["output"]})
        except Exception as e:
            _reply({"status": "error", "msg": str(e), "tb": traceback.format_exc()})


if __name__ == "__main__":
    main()
