FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Install Python 3.10 + system dependencies.
#
# Retries/timeouts are not optional here: this layer pulls ~270 packages
# (ffmpeg's dependency tree is most of it) over plain HTTP from
# archive.ubuntu.com, and a single dropped connection aborts a build that
# takes ~15 minutes to reach this point. A CI run failed exactly that way on
# one .deb after 868 seconds.
#
# Deliberately NOT using --no-install-recommends: it would shrink the image,
# but ffmpeg's recommended packages carry optional codecs, and a missing codec
# fails at runtime in the video pipeline rather than at build time — the worst
# place to discover it.
RUN apt-get update -o Acquire::Retries=5 \
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
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf python3.10 /usr/bin/python3 \
    && ln -sf python3 /usr/bin/python \
    && ln -sf pip3 /usr/bin/pip

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
