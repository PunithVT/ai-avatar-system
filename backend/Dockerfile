FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Install system dependencies. Python itself comes from uv, below.
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
                ca-certificates \
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
    rm -rf /var/lib/apt/lists/*

# Python 3.11, in a virtualenv.
#
# 3.11, not Ubuntu 22.04's default 3.10: 3.10 is past end-of-life, and the
# fixed releases of Pillow and filelock (57 advisories between them) need
# 3.11. And NOT Ubuntu 22.04's own python3.11 package either — that is
# 3.11.0rc1, a 2022 release candidate that never received a single fix.
# uv installs a current, maintained 3.11 build (the same one the test suite
# runs on), independent of what the base image's distribution happens to ship.
ARG UV_VERSION=0.12.8
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN set -eux; \
    curl -fsSL --retry 5 --retry-delay 5 \
        "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
        | tar -xz -C /usr/local/bin --strip-components=1; \
    uv python install 3.11; \
    uv venv --seed --python 3.11 /opt/venv; \
    /opt/venv/bin/python --version

# Everything below — pip, python, alembic, uvicorn, celery — resolves to the
# virtualenv.
ENV PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv

# Install PyTorch with CUDA 11.8 (matches base image) FIRST.
#
# requirements.txt pins the same versions without a CUDA suffix. Installed in
# the other order, pip fetched the default PyPI build of torch — a different
# CUDA generation, with its multi-GB nvidia-* wheels — and then this step
# replaced torch but left those wheels behind in an earlier layer for good.
# With the +cu118 builds already present, `torch==2.2.0` in requirements.txt
# is satisfied and nothing is downloaded twice.
RUN pip install --no-cache-dir \
    torch==2.2.0+cu118 \
    torchvision==0.17.0+cu118 \
    torchaudio==2.2.0+cu118 \
    --index-url https://download.pytorch.org/whl/cu118

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p /tmp/avatars /tmp/videos /tmp/audio voice_profiles uploads models

# Entrypoint: run migrations then start server
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Expose port
EXPOSE 8000

# Health check — longer start-period for model loading on first boot
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Run application via entrypoint (handles migrations automatically)
# Through bash rather than relying on the file's execute bit: the development
# compose file bind-mounts the source over /app, replacing this script with
# the host's copy, whose mode depends on the checkout.
CMD ["bash", "./entrypoint.sh"]
