FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TTS_HOST=0.0.0.0 TTS_PORT=8080
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 studio && useradd --uid 1000 --gid 1000 --create-home studio
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY app.py control.py parser.py tts.py video.py generate.py cleanup.py phrases.json README.md LICENSE ./
COPY templates/ ./templates/
COPY static/ ./static/
RUN mkdir /app/cache /app/output && chown studio:studio /app/cache /app/output
USER 1000:1000
EXPOSE 8080
CMD ["python", "app.py"]

FROM runtime AS test
USER root
COPY requirements-dev.txt test_project.py ./
RUN python -m pip install --no-cache-dir -r requirements-dev.txt
USER 1000:1000
CMD ["python", "-m", "unittest", "-v", "test_project.py"]

FROM runtime AS production
