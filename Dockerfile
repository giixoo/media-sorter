FROM python:3.12-slim

# ffmpeg: thumbnails + browser previews of HEVC / 10-bit / HDR / MKV… (built with zimg for HDR tone-mapping)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .

ENV MEDIA_DIR=/media \
    PORT=8765 \
    PYTHONUNBUFFERED=1
EXPOSE 8765
VOLUME ["/media"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8765')+'/api/status',timeout=4)"
CMD ["python", "server.py"]
