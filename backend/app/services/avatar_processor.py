import asyncio
import logging
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from app.config import settings

logger = logging.getLogger(__name__)


class AvatarProcessor:
    """Process and prepare avatar images for animation"""

    def __init__(self):
        self.resolution = settings.AVATAR_RESOLUTION

    async def process_image(self, image_path: str, output_path: str) -> Tuple[str, dict]:
        """
        Process an uploaded avatar image.

        Runs in a worker thread. Decoding, face detection, resampling and
        JPEG encoding of an image up to 10 MB is hundreds of milliseconds of
        CPU; done inline in a coroutine it froze the event loop, stalling
        every live conversation on the server for the length of each upload.
        """
        return await asyncio.to_thread(self._process_image_sync, image_path, output_path)

    def _process_image_sync(self, image_path: str, output_path: str) -> Tuple[str, dict]:
        """
        Process uploaded avatar image

        Args:
            image_path: Path to input image
            output_path: Path to save processed image

        Returns:
            Tuple of (output_path, metadata)
        """
        try:
            logger.info(f"Processing avatar image: {image_path}")

            # Load image
            image = Image.open(image_path)

            # Convert to RGB
            if image.mode != "RGB":
                image = image.convert("RGB")

            # Get original dimensions
            orig_width, orig_height = image.size

            # Detect face and crop
            face_box = self._detect_face(np.array(image))

            if face_box:
                x, y, w, h = face_box
                # Add padding
                padding = int(min(w, h) * 0.3)
                x = max(0, x - padding)
                y = max(0, y - padding)
                w = min(orig_width - x, w + 2 * padding)
                h = min(orig_height - y, h + 2 * padding)

                # Crop to face
                image = image.crop((x, y, x + w, y + h))
                logger.info(f"Face detected and cropped: {face_box}")
            else:
                logger.warning("No face detected, using center crop")
                # Center crop
                size = min(orig_width, orig_height)
                left = (orig_width - size) // 2
                top = (orig_height - size) // 2
                image = image.crop((left, top, left + size, top + size))

            # Resize to target resolution
            image = image.resize((self.resolution, self.resolution), Image.Resampling.LANCZOS)

            # Enhance image
            image = self._enhance_image(image)

            # Save processed image
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            image.save(output_path, quality=95)

            # Create thumbnail
            # Derived from the file name only. str.replace(".", ...) rewrote
            # EVERY dot in the path, so a temp directory containing one
            # produced a path into a directory that does not exist.
            out = Path(output_path)
            thumbnail_path = str(out.with_name(f"{out.stem}_thumb{out.suffix}"))
            thumbnail = image.copy()
            thumbnail.thumbnail((256, 256), Image.Resampling.LANCZOS)
            thumbnail.save(thumbnail_path, quality=85)

            metadata = {
                "original_size": (orig_width, orig_height),
                "processed_size": (self.resolution, self.resolution),
                "face_detected": face_box is not None,
                "thumbnail_path": thumbnail_path,
            }

            logger.info(f"Avatar processed successfully: {output_path}")
            return output_path, metadata

        except Exception as e:
            logger.error(f"Failed to process avatar image: {e}")
            raise

    def _detect_face(self, image: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
        """Detect face in image using OpenCV"""
        try:
            # Load face cascade
            face_cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )

            # Convert to grayscale
            gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

            # Detect faces
            faces = face_cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(100, 100)
            )

            if len(faces) > 0:
                # Return largest face
                return tuple(max(faces, key=lambda f: f[2] * f[3]))

            return None

        except Exception as e:
            logger.warning(f"Face detection error: {e}")
            return None

    def _enhance_image(self, image: Image.Image) -> Image.Image:
        """Enhance image quality"""
        try:
            from PIL import ImageEnhance

            # Slightly enhance sharpness
            enhancer = ImageEnhance.Sharpness(image)
            image = enhancer.enhance(1.1)

            # Slightly enhance contrast
            enhancer = ImageEnhance.Contrast(image)
            image = enhancer.enhance(1.05)

            return image

        except Exception as e:
            logger.warning(f"Image enhancement error: {e}")
            return image


# Global instance
avatar_processor = AvatarProcessor()
