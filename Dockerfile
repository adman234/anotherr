# Anotherr - media QA for Unraid / Plex / *arr
# CUDA 12.8 + cuDNN 9 runtime: required for faster-whisper on RTX 50-series (Blackwell) GPUs.
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/venv/bin:$PATH \
    ANOTHERR_CONFIG=/config \
    HOME=/config \
    HF_HOME=/config/models/hf \
    PUID=99 PGID=100 TZ=Etc/UTC

# If the Ubuntu archive is unreachable from the build machine, retry once on another mirror.
RUN inst() { apt-get update -o Acquire::http::Timeout=20 -o Acquire::Retries=2 \
      && apt-get install -y --no-install-recommends -o Acquire::Retries=2 \
         python3 python3-venv ffmpeg tzdata ca-certificates util-linux \
         tesseract-ocr tesseract-ocr-eng mkvtoolnix; } \
 && { inst || { sed -i -E 's#https?://(archive|security)\.ubuntu\.com/ubuntu/?#http://mirrors.edge.kernel.org/ubuntu/#g' \
        /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list 2>/dev/null; inst; }; } \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY anotherr /app/anotherr
COPY mcp/anotherr_mcp.py /app/anotherr/mcp_shim.py
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 9797
VOLUME ["/config"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9797/healthz', timeout=5)" || exit 1
# Which build is running (commit + build time), shown on the dashboard and in /api/status.
# Last, so it never invalidates the cached layers above.
ARG ANOTHERR_BUILD=dev
ENV ANOTHERR_BUILD=$ANOTHERR_BUILD
ENTRYPOINT ["/entrypoint.sh"]
