FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py auth_backend.py account_auth.py interop_constants.py direct_client.py stream_service.py motion_recorder.py ./

ENTRYPOINT ["python3", "/app/bot.py"]
CMD ["serve"]
