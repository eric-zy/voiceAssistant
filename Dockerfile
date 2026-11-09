# The voice assistant server. CPU only: speech recognition (SenseVoice) runs here; cloned voices (CosyVoice) are
# off in this image. Models and user data are volumes, not baked in: see deploy/README.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# libgomp: OpenMP for onnxruntime and ctranslate2; PyAV's wheel brings its own FFmpeg.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY *.py ./
COPY static ./static

# Run as a normal user; data and models are mounted and must be writable / readable by it.
RUN useradd --create-home --uid 10001 voice && mkdir -p /app/data /app/models && chown -R voice /app/data /app/models
USER voice

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4).status == 200 else 1)"

# --proxy-headers with nginx in front, so rate limits see the visitor's address, not nginx's.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port 8765 --ws-max-size 2097152 --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-*}"]
