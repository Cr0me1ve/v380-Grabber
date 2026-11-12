FROM python:3.13-slim-bookworm AS dependencies
RUN pip install --no-cache-dir --target /deps cryptography==50.0.2
# Reuse the exact FFmpeg/Python base already used by the web/archive service.
FROM ghcr.io/motioneye-project/motioneye@sha256:7da31d62e6c46ee8b61280f872801e429ec6f3db81bb23aca11c83863939cade
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/v380-python
USER root
COPY --from=dependencies /deps /opt/v380-python
WORKDIR /app
COPY direct_client.py stream_service.py account_auth.py interop_constants.py ./
COPY runtime/ ./runtime/
ARG V380_REVISION=local
LABEL org.opencontainers.image.revision=$V380_REVISION
USER 1000:1000
ENTRYPOINT ["python3", "/app/stream_service.py"]
