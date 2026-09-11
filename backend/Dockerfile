FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Install Python 3.10 + system dependencies.
#
# Wrapped in a retry loop because this layer is the build's most fragile step:
# it runs right after the multi-GB CUDA base image is pulled, then fetches
# ~270 packages (mostly ffmpeg's dependency tree) over plain HTTP. Three
# consecutive CI runs failed here against three different archive.ubuntu.com
# mirror IPs — sometimes on a package, once on the index fetch itself — after
# ~9 minutes of work that then had to be thrown away.
#
# Acquire::Retries alone was not enough: it retries within a single apt
# invocation, so a mirror that is unreachable for a stretch still fails the
# whole command. Retrying the command itself, with a pause between attempts,
# rides out that window.
#
# Deliberately NOT pinning an Azure/region-specific mirror: it would suit
# GitHub's runners but make the Dockerfile worse to build anywhere else.
# Deliberately NOT using --no-install-recommends: it would shrink the image,
# but ffmpeg's recommended packages carry optional codecs, and a missing codec
# surfaces at runtime in the video pipeline rather than at build time.
RUN set -eux; \
    apt_ok=0; \
    for attempt in 1 2 3 4 5; do \
        if apt-get update -o Acquire::Retries=5 -o Acquire::http::Timeout=30 \
            && apt-get install -y \
                -o Acquire::Retries=5 \
                -o Acquire::http::Timeout=30 \
                python3.10 \
                python3.10-dev \
                python3-pip \
                ffmpeg \
                libsm6 \
                libxext6 \
                libxrender-dev \
                libgomp1 \
                git \
                wget \
                curl; \
        then apt_ok=1; break; fi; \
        echo "apt attempt ${attempt} failed; retrying in $((attempt * 15))s" >&2; \
        sleep $((attempt * 15)); \
    done; \
    [ "$apt_ok" = 1 ] || { echo "apt failed after 5 attempts" >&2; exit 1; }; \
    rm -rf /var/lib/apt/lists/*; \
    ln -sf python3.10 /usr/bin/python3; \
    ln -sf python3 /usr/bin/python; \
    ln -sf pip3 /usr/bin/pip

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install PyTorch with CUDA 11.8 (matches base image)
RUN pip install --no-cache-dir \
    torch==2.2.0+cu118 \
    torchvision==0.17.0+cu118 \
    torchaudio==2.2.0+cu118 \
    --index-url https://download.pytorch.org/whl/cu118

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p /tmp/avatars /tmp/videos /tmp/audio voice_profiles

# Entrypoint: run migrations then start server
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Expose port
EXPOSE 8000

# Health check — longer start-period for model loading on first boot
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Run application via entrypoint (handles migrations automatically)
CMD ["./entrypoint.sh"]
