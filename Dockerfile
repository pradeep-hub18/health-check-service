FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir --upgrade pip==26.2.2 \
    && python3 -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 healthcheck \
    && useradd --uid 10001 --gid healthcheck --no-create-home --shell /usr/sbin/nologin healthcheck

COPY --chown=10001:10001 app/ ./app/
COPY --chown=10001:10001 run.py .
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
CMD ["python3", "run.py"]
