FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app/ ./app/
COPY README.md ./

RUN groupadd --gid 10001 botuser \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin botuser \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data

USER 10001:10001

CMD ["python", "-m", "app.main"]
