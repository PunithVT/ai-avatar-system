from pathlib import Path
from typing import List, Optional, Union

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

# Resolve .env path relative to this file's location (always project root)
_ENV_FILE = str(Path(__file__).resolve().parent.parent.parent / ".env")

_WEAK_SECRETS = {"change-this-secret-key", "change-this-jwt-secret", "change-this-jwt-secret-key"}


class Settings(BaseSettings):
    # Application
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str

    # Database
    DATABASE_URL: str = "postgresql://avatar_user:password@localhost:5432/avatar_db"
    DATABASE_NAME: str = "avatar_db"
    DATABASE_USER: str = "avatar_user"
    DATABASE_PASSWORD: str = "password"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Storage — local by default; set USE_LOCAL_STORAGE=false to use S3
    USE_LOCAL_STORAGE: bool = True
    LOCAL_STORAGE_PATH: str = "uploads"

    # AWS (only needed when USE_LOCAL_STORAGE=false)
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "us-east-1"
    S3_BUCKET_NAME: str = "avatar-system-storage"
    CLOUDFRONT_DOMAIN: Optional[str] = None

    # API Keys
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""

    # Point the OpenAI-compatible client at a different server — Ollama
    # (http://localhost:11434/v1), vLLM, LM Studio, OpenRouter, etc.
    # Used when LLM_PROVIDER is "openai" or "ollama".
    OPENAI_BASE_URL: Optional[str] = None

    # LLM Configuration
    # Claude: claude-sonnet-5-5 (default — fast and capable, the right fit for
    # spoken conversation), claude-opus-5-5 (most capable Opus, slower to the
    # first word), claude-haiku-5-5 (fastest and cheapest). Older models still
    # work; llm.py sends each generation the parameters it accepts.
    # OpenAI users should override LLM_MODEL via .env (e.g. gpt-4o).
    # "ollama" runs fully local & free: set LLM_MODEL to e.g. llama3.1 and
    # optionally OPENAI_BASE_URL (defaults to http://localhost:11434/v1).
    LLM_PROVIDER: str = "anthropic"  # anthropic, openai, ollama
    LLM_MODEL: str = "claude-sonnet-5-5"
    # OpenAI-compatible providers only. The Claude API no longer accepts
    # sampling parameters, so this is never sent to Anthropic.
    LLM_TEMPERATURE: float = 0.7
    LLM_MAX_TOKENS: int = 2000
    # Claude 4.7 and later only: how hard the model works before answering
    # (low | medium | high | xhigh | max). "low" keeps the pause before the
    # avatar starts speaking short; raise it if replies need more reasoning
    # and the delay is acceptable. Empty uses the model's own default.
    LLM_EFFORT: str = "low"
    # For OpenAI-compatible reasoning ("thinking") models — notably Ollama
    # cloud models like glm-*:cloud / kimi-*:cloud — set to "none" to disable
    # the reasoning phase. Without it those models emit their text as
    # `reasoning` and leave `content` empty, so the real-time pipeline gets
    # nothing. Forwarded verbatim in the request body; leave empty for normal
    # (non-reasoning) models so nothing is sent.
    LLM_REASONING_EFFORT: str = ""

    # Face restoration (optional, GPU only).
    #
    # MuseTalk regenerates the mouth region at 256x256 and the worker scales it
    # back up to the crop size, while avatars themselves are stored at
    # AVATAR_RESOLUTION (512). The mouth is therefore rendered at roughly half
    # the resolution of the face around it, which is the pipeline's visible
    # quality ceiling — a model swap does not fix it, a restorer does.
    #
    # Off by default: restoration runs per frame and costs real time, so
    # whether it is worth the FPS has to be measured on the target GPU rather
    # than assumed. See README -> Face Restoration.
    FACE_RESTORE: str = "off"  # off | gfpgan
    # Downloaded by scripts/setup_face_restore.sh. Relative paths resolve
    # against MUSETALK_PATH, since the worker runs with that as its cwd.
    FACE_RESTORE_MODEL: str = "models/gfpgan/GFPGANv1.4.pth"

    # Avatar Engine
    #   musetalk   — default. Persistent worker, ~30 FPS on a 16-24 GB GPU.
    #   liveavatar — Alibaba LiveAvatar (Wan2.2-S2V-14B + LoRA). Higher fidelity
    #                and unbounded clip length, but needs 48 GB VRAM with FP8 /
    #                80 GB without, and its inference script loads the 14B model
    #                per invocation. See LIVEAVATAR_PATH below.
    #   simple     — ffmpeg static image + audio, no lip-sync. CPU, instant.
    AVATAR_ENGINE: str = "musetalk"  # musetalk, liveavatar, simple

    # LiveAvatar. Only consulted when AVATAR_ENGINE=liveavatar.
    #
    # Its entry point (minimal_inference/s2v_streaming_interact.py) runs once
    # and exits, with no persistent-worker mode — so every turn pays a full 14B
    # model load. That is the opposite of why MuseTalk runs as a worker, and it
    # rules LiveAvatar out of the live WebSocket path today. It suits the
    # offline Celery render path, where a slow, better-looking result is the
    # point. See README -> LiveAvatar.
    LIVEAVATAR_PATH: str = "models/LiveAvatar"
    LIVEAVATAR_CKPT_DIR: str = "ckpt/Wan2.2-S2V-14B"
    LIVEAVATAR_LORA: str = "Quark-Vision/Live-Avatar"
    # FP8 halves the weights and is what brings this within reach of a 48 GB
    # card; without it the model wants 80 GB.
    LIVEAVATAR_FP8: bool = True
    LIVEAVATAR_SAMPLE_STEPS: int = 4
    LIVEAVATAR_INFER_FRAMES: int = 48
    LIVEAVATAR_SIZE: str = "480*832"
    # A single turn on one GPU is minutes, not seconds.
    LIVEAVATAR_TIMEOUT_SECS: int = 1800
    AVATAR_RESOLUTION: int = 512
    AVATAR_FPS: int = 25
    # Lip-sync frames onto the WebRTC stream as they are generated, instead
    # of after the whole sentence has been rendered, encoded to MP4 and
    # decoded again. Only with MEDIA_TRANSPORT=webrtc and the musetalk engine.
    #
    # What it buys is the wait before each sentence's first frame: one batch
    # of frames instead of all of them plus an encode and a decode. What it
    # risks is a sentence that starts and then stalls, if the engine produces
    # frames slower than they are played — so a sentence is only started
    # early while the engine's measured rate is comfortably above AVATAR_FPS
    # (see /health -> lipsync_fps); otherwise its frames are held until it is
    # complete, which is no worse than leaving this off. Off by default: it
    # has been exercised on CPU, where the engine never keeps up, and not yet
    # on a GPU, where it is meant to matter.
    LIPSYNC_PROGRESSIVE: bool = False
    MUSETALK_PATH: str = "models/MuseTalk"

    # STT Configuration
    # large-v3-turbo: best 2026 sweet spot — ~216x real-time on GPU, multilingual,
    # only ~1% lower WER than large-v3. Falls back to base/small if VRAM is tight.
    STT_PROVIDER: str = "whisper"  # whisper, google, azure
    WHISPER_MODEL: str = "large-v3-turbo"  # tiny, base, small, medium, large-v3, large-v3-turbo
    # Decoding beam width. 1 (greedy) is the usual choice for live speech: on
    # the turbo model it is markedly faster than a beam of 5 for a small
    # accuracy cost, and transcription sits directly in front of every voice
    # reply. Raise it if accuracy on your audio matters more than latency.
    WHISPER_BEAM_SIZE: int = 1

    # TTS Configuration
    # chatterbox: Resemble AI's open-source TTS — voice cloning, 23 languages.
    #             Needs requirements-tts.txt; without it synthesis uses Edge TTS.
    # edge:       Microsoft neural voices. No GPU, no key, no cloning. Pick this
    #             when Chatterbox is not installed.
    # gtts:       Google TTS only (lowest quality; the last-resort fallback).
    # supertonic: Supertonic 3 — a local ONNX model on the CPU, so speech needs
    #             no network (Edge TTS is an unofficial endpoint that can stop
    #             working, and takes ~0.8 s to answer). Preset voices only,
    #             no cloning. Speaks ar da de el en es fi fr hi it ja ko nl pl
    #             pt ru sv tr; sessions in he, ms, no, sw or zh, and any
    #             sentence with characters it does not know, use the Edge ->
    #             gTTS chain instead.
    #             Optional, not the default, for three reasons to accept
    #             knowingly: its weights (~400 MB) are downloaded from Hugging
    #             Face on first use, into ~/.cache/supertonic3 or
    #             SUPERTONIC_CACHE_DIR; they are licensed BigScience
    #             OpenRAIL-M — royalty-free and redistributable, but with use
    #             restrictions you must pass on to your own users; and the
    #             upstream repository is archived, so there will be no fixes.
    TTS_PROVIDER: str = "chatterbox"  # chatterbox, edge, gtts, supertonic
    # Supertonic's preset voice: F1-F5 or M1-M5.
    SUPERTONIC_VOICE: str = "F1"
    # Denoising steps per sentence — the quality/speed trade. 5 synthesises
    # at roughly 0.2-0.3x real time on a laptop CPU; the package's own
    # default of 8 sounds a little cleaner and takes about half as long again.
    SUPERTONIC_STEPS: int = 5

    # Security
    # Union[..., str] lets pydantic-settings keep a non-JSON env value as a
    # raw string instead of failing the JSON parse, so both formats work:
    #   CORS_ORIGINS=["http://a","http://b"]   (JSON)
    #   CORS_ORIGINS=http://a,http://b         (comma-separated)
    CORS_ORIGINS: Union[List[str], str] = ["http://localhost:3000", "http://localhost:8000"]
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_HOURS: int = 24

    # Auth cookie. Login sets the JWT in an httpOnly cookie (not readable by
    # JS → not stealable via XSS). `Secure` should be true in production
    # (HTTPS only); SameSite=lax works across same-site ports (localhost:3000
    # → :8000 in dev) and blocks cross-site POST, which covers most CSRF.
    AUTH_COOKIE_NAME: str = "access_token"
    AUTH_COOKIE_SECURE: bool = False  # set true in production (.env.prod)
    AUTH_COOKIE_SAMESITE: str = "lax"  # lax | strict | none
    AUTH_COOKIE_DOMAIN: Optional[str] = None

    # Conversation memory.
    #
    # The LLM context is a fixed window of the most recent turns; anything
    # older is dropped, so a long conversation silently forgets its own
    # beginning. With this on, the turns falling out of the window are rolled
    # into a running summary and carried in the system prompt instead.
    #
    # Deliberately a summary rather than embeddings + retrieval: it costs one
    # LLM call when the window overflows (not per turn), needs no vector store
    # or embedding model, and works with every provider the project supports —
    # including Ollama, where requiring an embeddings API would have broken the
    # "runs fully local and free" path.
    CONVERSATION_MEMORY: bool = True
    # Cap on the carried summary. Long enough to hold the thread of a
    # conversation, short enough that it cannot crowd out the recent turns it
    # exists to supplement.
    MEMORY_SUMMARY_MAX_CHARS: int = 1500

    # How the avatar's picture and voice reach the browser.
    #   clips  — default. One MP4 per sentence over HTTP; the browser plays
    #            them back to back. Works anywhere HTTP works, but every
    #            sentence boundary restarts the decoder (a visible stutter).
    #   webrtc — one continuous audio+video stream per session; clips are
    #            decoded here and fed into it, so the picture never tears
    #            down and an interruption takes effect immediately.
    #
    # webrtc has a deployment cost, which is why it is opt-in: media travels
    # over UDP straight between the browser and THIS process, not through the
    # HTTP proxy. The backend must be reachable from browsers on UDP, and a
    # container on a default Docker bridge network is not. There are two
    # ways to make it so:
    #   - run the backend with host networking (network_mode: host), or
    #   - put a TURN relay in WEBRTC_ICE_SERVERS and let media go through it.
    # Publishing a UDP port range is NOT one of them: each peer connection
    # binds whatever ephemeral port the kernel hands it, and neither aiortc
    # nor this app has a setting that confines that to a range.
    # Users behind strict/symmetric NATs need the TURN relay either way.
    #
    # A session that cannot connect stays on clips. One whose stream fails
    # later goes back to clips, and the sentences the stream had not finished
    # playing are re-sent as clips — what is lost is at most what was "played"
    # into a connection that had already died but not yet been noticed (the
    # browser reports that within a few seconds; see rtcSession.ts).
    #
    # It also costs CPU and memory per connected session, speaking or not:
    #   - encoding: the stream never stops. Up to about a third of a core
    #     while the avatar speaks (512x512 at 25 fps). An idle stream drops
    #     its video to 5 fps after a second, which brings that to roughly a
    #     fifth of a core — mostly the audio track — for as long as the tab
    #     stays open.
    #   - memory: decoded frames are ~10 MB per queued second. Typically
    #     50-100 MB while speaking, ~235 MB worst case (see MAX_QUEUE_SECONDS
    #     and MAX_CLIP_SECONDS in services/webrtc.py), nothing when idle.
    # Size the host for the number of simultaneous sessions accordingly.
    MEDIA_TRANSPORT: str = "clips"  # clips | webrtc
    # STUN/TURN servers, as a JSON list in the browser's RTCIceServer shape.
    # Used by the server's peer connection AND sent to the browser, so list
    # only what a client may see. STUN (the default) lets each side discover
    # its public address; it relays nothing. A TURN entry carries credentials:
    #   [{"urls": ["turn:turn.example.com:3478"], "username": "u", "credential": "p"}]
    # A malformed value is logged and treated as "no servers", not fatal.
    WEBRTC_ICE_SERVERS: str = '[{"urls": ["stun:stun.l.google.com:19302"]}]'

    # Who decides when the user has finished speaking, in hands-free mode on
    # the WebRTC transport. There the microphone travels up the same peer
    # connection as the avatar comes down, and the server takes the turns
    # (services/turn_taking.py) instead of the browser's loudness detector.
    #   smart — default. Silero VAD finds speech; when the speaker pauses, the
    #           Smart Turn model judges whether the sentence sounds finished.
    #           A complete thought is answered after VAD_STOP_SECS; a pause
    #           mid-sentence is waited through. Costs ~0.1 s of one CPU core
    #           per pause, and two small ONNX models per listening session.
    #   vad   — Silero VAD only: a turn ends after VAD_STOP_SECS of silence.
    #           Nothing to wait for and nothing to misjudge, but a pause for
    #           thought is taken as the end.
    #   off   — the microphone is not offered over WebRTC; hands-free stays
    #           in the browser, exactly as on the clips transport.
    # Has no effect unless MEDIA_TRANSPORT=webrtc. Needs the pipecat-ai
    # package; without it this behaves as "off".
    VOICE_TURN_DETECTION: str = "smart"  # smart | vad | off
    # Silence, in seconds, before the VAD reports that speech has stopped.
    # In "vad" mode that ends the turn; in "smart" mode it is only the pause
    # after which the turn model is asked. 0 picks the default for the mode:
    #   smart — 0.2, Pipecat's VAD default and the value Smart Turn is tuned
    #           against. Longer delays every reply by the difference.
    #   vad   — 0.8 (Pipecat's 0.2 plus its 0.6 s speech timeout). Shorter
    #           answers faster and cuts more people off mid-sentence.
    VAD_STOP_SECS: float = 0.0
    # "smart" mode: how long to wait for more after a pause the turn model
    # judged unfinished, before answering anyway. Longer is more patient with
    # slow speakers; it is also how long a turn the model misjudged hangs.
    TURN_MAX_WAIT_SECS: float = 3.0
    # Show the user's words while they are still speaking, by transcribing
    # the utterance so far every STT_PARTIAL_INTERVAL_SECS. Each one is a
    # full Whisper pass over everything said so far, so:
    #   auto — default: only when speech-to-text runs on a GPU, where a pass
    #          takes a fraction of the interval.
    #   on   — always. On CPU the passes queue up behind each other and the
    #          final transcript waits for the one in flight.
    #   off  — never; the transcript appears once, when the turn ends.
    STT_PARTIALS: str = "auto"  # auto | on | off
    # Shorter feels more live and costs proportionally more transcription.
    STT_PARTIAL_INTERVAL_SECS: float = 1.5

    # Streaming speech-to-text for that same microphone (hands-free on the
    # WebRTC transport, with the server taking the turns). Nothing else uses
    # it: push-to-talk and the clips transport send recordings, to Whisper.
    #   off    — default. Whisper transcribes each utterance when it ends.
    #   sherpa — sherpa-onnx running NVIDIA Nemotron 3.5 ASR Streaming 0.6B.
    #            The recognizer hears the audio as it arrives, so the user's
    #            words appear while they speak (a few times a second, on CPU)
    #            and the final transcript is ready ~0.2 s after the last
    #            word instead of after a whole Whisper pass. Whisper's
    #            speculative pass and STT_PARTIALS are not used for these
    #            utterances.
    # What it costs: a ~630 MB model held in RAM for the life of the process
    # (one copy, shared by every session), and CPU for as long as someone is
    # speaking — about a quarter of real time across STT_STREAMING_THREADS
    # cores per speaking user. All decoding shares one thread, so a handful
    # of simultaneous speakers keep up; beyond that, new utterances go to
    # Whisper rather than queue.
    # Languages: the model is only used where it is accurate (en, es, fr, it,
    # pt, nl, de, tr, ru, ar, hi, ja, ko, vi, uk). A session in any other
    # language keeps using Whisper, as does every utterance if the package
    # or the model is missing, or the engine fails mid-sentence.
    STT_STREAMING: str = "off"  # off | sherpa
    # Downloaded by scripts/setup_streaming_stt.sh (475 MB; not in the
    # repository or the image). Relative paths are tried as given and then
    # against backend/, like MUSETALK_PATH.
    STT_STREAMING_MODEL_DIR: str = (
        "models/stt/sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-560ms-int8-2026-06-11"
    )
    # Cores one decode may use. More shortens each decode until the machine
    # runs out of them; the animator and TTS want cores too.
    STT_STREAMING_THREADS: int = 4

    # Guest accounts. "Continue as Guest" mints a real (anonymous) user row
    # so the ordinary per-user ownership checks scope a guest's data to just
    # that guest. They are disposable: the daily cleanup task deletes guest
    # users whose sessions have all been idle longer than this.
    GUEST_ACCOUNTS_ENABLED: bool = True
    GUEST_RETENTION_HOURS: int = 48

    # Generated video clips are one spoken sentence each and are useless once
    # played — the transcript is the record. The daily cleanup deletes clips
    # older than this from LOCAL storage. On S3, set a lifecycle rule on the
    # videos/ prefix instead (infrastructure/main.tf does). 0 keeps them forever.
    MEDIA_RETENTION_HOURS: int = 24

    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = 60
    RATE_LIMIT_PER_HOUR: int = 1000

    # Load the speech and lip-sync models when the server starts instead of on
    # the first turn. Off by default so development restarts stay quick; turn
    # it on wherever a real person is the first to speak after a deploy —
    # otherwise that person waits a minute or more for the models.
    PRELOAD_MODELS: bool = False

    # WebSocket
    # Turns (text or audio) one user may START per minute across all their
    # sessions. Each one is an LLM call and, for audio, a transcription. The
    # HTTP rate limiter does not see WebSocket frames, so this is the only
    # bound on that path. 0 disables it.
    WS_TURNS_PER_MINUTE: int = 30
    # Largest decoded audio message accepted. Hands-free turns are capped at
    # 30 s in the browser, which is well under 1 MB of Opus.
    WS_MAX_AUDIO_BYTES: int = 8 * 1024 * 1024
    WS_MAX_CONNECTIONS: int = 1000
    WS_PING_INTERVAL: int = 30
    WS_PING_TIMEOUT: int = 10

    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # File Upload. Enforced while streaming the request body (see
    # app.api.v1.avatars._read_capped) so an oversized upload is rejected
    # without ever being buffered in full.
    MAX_UPLOAD_SIZE: int = 10485760  # 10MB
    ALLOWED_EXTENSIONS: Union[List[str], str] = ["jpg", "jpeg", "png", "webp"]
    # Upper bound on the free-text avatar name accepted at upload time —
    # matches AvatarRename.name so both entry points agree.
    MAX_AVATAR_NAME_LEN: int = 200
    # Voice samples are 10-60 s of audio, so they need more headroom than a
    # still image. Enforced the same way, while streaming the body.
    MAX_VOICE_UPLOAD_SIZE: int = 20971520  # 20MB

    # Monitoring
    SENTRY_DSN: Optional[str] = None
    PROMETHEUS_ENABLED: bool = True

    # Distributed tracing (OpenTelemetry). Off by default — when enabled,
    # requires the optional `opentelemetry-*` packages (see
    # requirements-otel.txt). Spans are no-ops when disabled, so the
    # instrumentation in the hot path costs nothing in the default build.
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "avatar-backend"
    # OTLP/gRPC collector endpoint, e.g. "http://otel-collector:4317".
    OTEL_EXPORTER_OTLP_ENDPOINT: Optional[str] = None

    # URLs
    FRONTEND_URL: str = "http://localhost:3000"
    BACKEND_URL: str = "http://localhost:8000"

    @field_validator("CORS_ORIGINS", "ALLOWED_EXTENSIONS", mode="before")
    @classmethod
    def _split_comma_separated(cls, value):
        """Accept comma-separated env strings (.env.example style) as lists."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _validate_production_hardening(self) -> "Settings":
        """
        Fail fast when a production deployment is configured unsafely.

        DEBUG is the single switch that unlocks several dev-only behaviours:
        interactive API docs, `create_all` instead of Alembic, raw exception
        text in 5xx bodies, and SQL echo (which logs statement parameters).
        A deploy that sets ENVIRONMENT=production but leaves DEBUG at its
        `True` default silently gets all of them, so refuse to start rather
        than run a production service in that state.
        """
        if self.ENVIRONMENT != "production":
            return self

        problems: list[str] = []
        if self.DEBUG:
            problems.append("DEBUG must be false in production")
        if not self.AUTH_COOKIE_SECURE:
            problems.append(
                "AUTH_COOKIE_SECURE must be true in production so the auth "
                "cookie is never sent over plain HTTP"
            )
        if "*" in self.CORS_ORIGINS:
            problems.append(
                "CORS_ORIGINS must not contain '*' in production — list your exact origins instead"
            )
        if any(str(o).startswith("http://") for o in self.CORS_ORIGINS):
            problems.append("CORS_ORIGINS must use https:// in production")
        if problems:
            raise ValueError("Unsafe production configuration:\n  - " + "\n  - ".join(problems))
        return self

    @model_validator(mode="after")
    def _validate_secrets(self) -> "Settings":
        for field, value in (
            ("SECRET_KEY", self.SECRET_KEY),
            ("JWT_SECRET_KEY", self.JWT_SECRET_KEY),
        ):
            if value in _WEAK_SECRETS:
                raise ValueError(
                    f"{field} is set to an insecure default — set a strong random value in .env"
                )
            if len(value) < 32:
                raise ValueError(f"{field} must be at least 32 characters")
        return self

    model_config = {
        "env_file": _ENV_FILE,
        "case_sensitive": True,
        # Ignore (rather than reject) unrecognised keys. Operators carry
        # long-lived .env files; a setting retired from this class must not
        # turn their existing file into a startup crash.
        "extra": "ignore",
    }


settings = Settings()
