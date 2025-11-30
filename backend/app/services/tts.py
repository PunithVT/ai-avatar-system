from TTS.api import TTS
import logging
import io
import numpy as np
import soundfile as sf
import torch
from typing import Optional
from pathlib import Path
from app.config import settings

logger = logging.getLogger(__name__)


class TTSService:
    """Text-to-Speech Service"""
    
    def __init__(self):
        self.provider = settings.TTS_PROVIDER
        self.model = None
        
    def _check_cuda(self) -> bool:
        """Check if CUDA is available"""
        try:
            return torch.cuda.is_available()
        except:
            return False
    
    async def initialize(self):
        """Initialize TTS model"""
        if self.model is not None:
            return
        
        try:
            if self.provider == "coqui":
                logger.info("Loading Coqui TTS model...")
                # Use XTTS v2 for high-quality multilingual TTS
                self.model = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
                
                # Move to GPU if available
                if self._check_cuda():
                    self.model.to("cuda")
                    logger.info("Coqui TTS loaded on GPU")
                else:
                    logger.info("Coqui TTS loaded on CPU")
            
            logger.info("TTS model loaded successfully")
        
        except Exception as e:
            logger.error(f"Failed to load TTS model: {e}")
            # Fallback to simpler model
            try:
                logger.info("Loading fallback TTS model...")
                self.model = TTS("tts_models/en/ljspeech/tacotron2-DDC")
                if self._check_cuda():
                    self.model.to("cuda")
                logger.info("Fallback TTS model loaded")
            except Exception as fallback_error:
                logger.error(f"Failed to load fallback model: {fallback_error}")
                raise
    
    async def synthesize(
        self,
        text: str,
        output_path: str,
        speaker_wav: Optional[str] = None,
        language: str = "en"
    ) -> str:
        """
        Synthesize speech from text
        
        Args:
            text: Text to synthesize
            output_path: Output audio file path
            speaker_wav: Optional speaker reference for voice cloning
            language: Language code
        
        Returns:
            Path to generated audio file
        """
        try:
            if self.model is None:
                await self.initialize()
            
            logger.info(f"Synthesizing speech: {text[:100]}...")
            
            # Ensure output directory exists
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            
            # Synthesize speech
            if speaker_wav and hasattr(self.model, 'tts_to_file'):
                # Voice cloning with reference audio
                self.model.tts_to_file(
                    text=text,
                    speaker_wav=speaker_wav,
                    language=language,
                    file_path=output_path
                )
            else:
                # Standard synthesis
                self.model.tts_to_file(
                    text=text,
                    file_path=output_path
                )
            
            logger.info(f"Speech synthesized successfully: {output_path}")
            return output_path
        
        except Exception as e:
            logger.error(f"Speech synthesis error: {e}")
            raise
    
    async def synthesize_bytes(
        self,
        text: str,
        speaker_wav: Optional[str] = None,
        language: str = "en"
    ) -> bytes:
        """
        Synthesize speech and return as bytes
        
        Args:
            text: Text to synthesize
            speaker_wav: Optional speaker reference
            language: Language code
        
        Returns:
            Audio data as bytes
        """
        try:
            # Create temporary file
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
                tmp_path = tmp_file.name
            
            # Synthesize to file
            await self.synthesize(text, tmp_path, speaker_wav, language)
            
            # Read file as bytes
            with open(tmp_path, 'rb') as f:
                audio_bytes = f.read()
            
            # Cleanup
            Path(tmp_path).unlink()
            
            return audio_bytes
        
        except Exception as e:
            logger.error(f"Failed to synthesize speech to bytes: {e}")
            raise
    
    async def get_available_speakers(self) -> list:
        """Get list of available speakers"""
        try:
            if self.model is None:
                await self.initialize()
            
            if hasattr(self.model, 'speakers'):
                return self.model.speakers
            return []
        
        except Exception as e:
            logger.error(f"Failed to get available speakers: {e}")
            return []


# Global instance
tts_service = TTSService()
