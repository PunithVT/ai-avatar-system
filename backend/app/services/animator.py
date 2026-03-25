import asyncio
import hashlib
import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import torch
import yaml

from app.config import settings

TMPDIR = Path(tempfile.gettempdir())

logger = logging.getLogger(__name__)


class AvatarAnimator:
    """
    Avatar Animation Service.
    Supported engines (set AVATAR_ENGINE in .env):
      - musetalk : MuseTalk V1.5 — best quality, Python 3.12 compatible (default)
      - simple   : ffmpeg static image + audio, no lip-sync
    """

    def __init__(self):
        self.engine = settings.AVATAR_ENGINE
        self.resolution = settings.AVATAR_RESOLUTION
        self.fps = settings.AVATAR_FPS
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self._initialised = False
        self._musetalk_dir: Optional[Path] = None

        logger.info(f"AvatarAnimator: engine={self.engine}, device={self.device}")

    # ── initialisation ────────────────────────────────────────────────────────

    async def initialize(self):
        if self._initialised:
            return

        if self.engine == "musetalk":
            self._musetalk_dir = self._find_dir(
                settings.MUSETALK_PATH, "scripts/inference.py"
            )
            if self._musetalk_dir is None:
                logger.warning(
                    "MuseTalk not found at '%s'. "
                    "Run scripts/setup_musetalk.sh to install it. "
                    "Falling back to simple animation.",
                    settings.MUSETALK_PATH,
                )
                self.engine = "simple"
            else:
                logger.info(f"MuseTalk found at: {self._musetalk_dir}")

        elif self.engine not in ("simple",):
            logger.warning(f"Unknown engine '{self.engine}', using simple animation.")
            self.engine = "simple"

        self._initialised = True

    def _find_dir(self, config_path: str, marker_file: str) -> Optional[Path]:
        """Return resolved path if marker_file exists inside it, else None."""
        candidates = [
            Path(config_path),
            Path(__file__).resolve().parent.parent.parent / config_path,
        ]
        for p in candidates:
            if (p / marker_file).exists():
                return p.resolve()
        return None

    # ── public API ────────────────────────────────────────────────────────────

    async def animate(
        self,
        avatar_image_path: str,
        audio_path: str,
        output_path: str,
        cache_key: Optional[str] = None,
    ) -> str:
        """
        Animate avatar with audio. Returns path to the generated video.
        Falls back to simple (static image + audio) on any engine failure.
        """
        if not self._initialised:
            await self.initialize()

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Animating [{self.engine}] image={avatar_image_path} audio={audio_path}")

        try:
            if self.engine == "musetalk":
                return await self._animate_musetalk(avatar_image_path, audio_path, output_path)
            else:
                return await self._animate_simple(avatar_image_path, audio_path, output_path)
        except Exception as e:
            logger.error(f"Animation failed ({self.engine}): {e}. Falling back to simple.")
            return await self._animate_simple(avatar_image_path, audio_path, output_path)

    # ── MuseTalk ──────────────────────────────────────────────────────────────

    async def _animate_musetalk(
        self,
        avatar_path: str,
        audio_path: str,
        output_path: str,
    ) -> str:
        """Run MuseTalk V1.5 via subprocess."""
        assert self._musetalk_dir is not None
        musetalk_dir: Path = self._musetalk_dir  # type: ignore[assignment]

        task_id = uuid.uuid4().hex
        output_name = f"musetalk_{task_id}.mp4"

        # MuseTalk reads a YAML config that maps task → {video_path, audio_path}
        cfg_path = TMPDIR / f"musetalk_cfg_{task_id}.yaml"
        cfg_data = {
            "task_0": {
                "video_path": str(Path(avatar_path).resolve()),
                "audio_path": str(Path(audio_path).resolve()),
                "bbox_shift": -7,
            }
        }
        cfg_path.write_text(yaml.dump(cfg_data))

        unet_model = musetalk_dir / "models" / "musetalkV15" / "unet.pth"
        unet_config = musetalk_dir / "models" / "musetalk" / "config.json"
        whisper_dir = musetalk_dir / "models" / "whisper"

        cmd = [
            sys.executable, "scripts/inference.py",
            "--inference_config", str(cfg_path),
            "--unet_model_path", str(unet_model),
            "--unet_config", str(unet_config),
            "--whisper_dir", str(whisper_dir),
            "--output_vid_name", output_name,
            "--version", "v15",
            "--batch_size", "4",
        ]
        if self.device == "cuda":
            cmd += ["--gpu_id", "0"]

        # Inject PYTHONPATH so the musetalk package (in the repo root) is importable
        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(musetalk_dir) + (":" + existing if existing else "")

        logger.info("Running MuseTalk: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._musetalk_dir),
            env=env,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")
            logger.error(f"MuseTalk stderr:\n{err}")
            raise RuntimeError(f"MuseTalk exited with code {proc.returncode}")

        # MuseTalk writes output to results/v15/<output_name>
        result_video = self._musetalk_dir / "results" / "v15" / output_name
        if not result_video.exists():
            candidates = sorted(
                (self._musetalk_dir / "results" / "v15").glob("*.mp4"),
                key=lambda f: f.stat().st_mtime,
            )
            if not candidates:
                raise FileNotFoundError("MuseTalk produced no output video")
            result_video = candidates[-1]

        shutil.move(str(result_video), output_path)
        cfg_path.unlink(missing_ok=True)

        logger.info(f"MuseTalk animation done: {output_path}")
        return output_path

    # ── Simple ffmpeg fallback ────────────────────────────────────────────────

    async def _animate_simple(
        self,
        avatar_path: str,
        audio_path: str,
        output_path: str,
    ) -> str:
        """Combine static image + audio with FFmpeg. No lip-sync."""
        logger.info("Using simple animation (static image + audio, no lip-sync)")

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(avatar_path),
            "-i", str(audio_path),
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            "-vf", (
                f"fps={self.fps},"
                f"scale={self.resolution}:{self.resolution}:"
                f"force_original_aspect_ratio=decrease,"
                f"pad={self.resolution}:{self.resolution}:(ow-iw)/2:(oh-ih)/2"
            ),
            output_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")
            logger.error(f"FFmpeg error:\n{err}")
            raise RuntimeError("Simple animation (ffmpeg) failed")

        logger.info(f"Simple animation done: {output_path}")
        return output_path

    # ── helpers ───────────────────────────────────────────────────────────────

    def generate_cache_key(self, text: str, avatar_id: str) -> str:
        return hashlib.md5(f"{avatar_id}:{text}".encode()).hexdigest()


# Global instance
avatar_animator = AvatarAnimator()
