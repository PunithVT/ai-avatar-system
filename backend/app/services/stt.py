import whisper
from faster_whisper import WhisperModel
import logging
import io
import numpy as np
import soundfile as sf
from typing import Union
from app.config import settings

logger = logging.getLogger(__name__)


class STTService:
    """Speech-to-Text Service"""
    
    def __init__(self):
        self.provider = settings.STT_PROVIDER
        self.model_name = settings.WHISPER_MODEL
        
        if self.provider == "whisper":
            # Use faster-whisper for better performance
            logger.info(f"Loading Whisper model: {self.model_name}")
            self.model = WhisperModel(
                self.model_name,
                device="cuda" if self._check_cuda() else "cpu",
                compute_type="float16" if self._check_cuda() else "int8"
            )
            logger.info("Whisper model loaded successfully")
    
    def _check_cuda(self) -> bool:
        """Check if CUDA is available"""
        try:
            import torch
            return torch.cuda.is_available()
        except:
            return False
    
    async def transcribe(
        self,
        audio_data: Union[bytes, str],
        language: str = "en"
    ) -> str:
        """Transcribe audio to text"""
        try:
            if self.provider == "whisper":
                return await self._transcribe_whisper(audio_data, language)
            else:
                raise ValueError(f"Unsupported STT provider: {self.provider}")
        
        except Exception as e:
            logger.error(f"Failed to transcribe audio: {e}")
            raise
    
    async def _transcribe_whisper(
        self,
        audio_data: Union[bytes, str],
        language: str = "en"
    ) -> str:
        """Transcribe using Whisper"""
        try:
            # Convert audio data to proper format
            if isinstance(audio_data, bytes):
                # Load audio from bytes
                audio, sample_rate = sf.read(io.BytesIO(audio_data))
            else:
                # Load audio from file path
                audio, sample_rate = sf.read(audio_data)
            
            # Convert to mono if stereo
            if len(audio.shape) > 1:
                audio = audio.mean(axis=1)
            
            # Resample to 16kHz if needed
            if sample_rate != 16000:
                import librosa
                audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16000)
            
            # Transcribe
            segments, info = self.model.transcribe(
                audio,
                language=language,
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500)
            )
            
            # Combine all segments
            transcription = " ".join([segment.text for segment in segments])
            
            logger.info(f"Transcription completed: {len(transcription)} characters")
            return transcription.strip()
        
        except Exception as e:
            logger.error(f"Whisper transcription error: {e}")
            raise
    
    async def transcribe_stream(
        self,
        audio_stream,
        language: str = "en"
    ):
        """Transcribe audio stream in real-time"""
        # TODO: Implement streaming transcription
        pass


# Global instance
stt_service = STTService()
