FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SERVICE_DATA_DIR=/data \
    HOME=/home/app

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libsndfile1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data /home/app/.cache/modelscope \
    && chown -R app:app /data /home/app

COPY requirements.txt ./
# CPU wheels avoid downloading the CUDA runtime for the default CPU worker.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchaudio \
    && pip install --no-cache-dir -r requirements.txt \
    && pip check \
    && python -c "import torch,torchaudio,funasr,modelscope,soundfile,openai,imageio_ffmpeg,subprocess; subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-version'], check=True)"

COPY --chown=app:app recorder.py polish.py service.py export_preview.py video_script.py llm.py ./
COPY --chown=app:app hotwords.txt ./
COPY --chown=app:app web ./web
USER app
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3).read()"
CMD ["python", "service.py", "--host", "0.0.0.0", "--port", "8765"]
