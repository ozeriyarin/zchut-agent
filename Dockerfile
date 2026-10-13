FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=8080
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && rm -rf src
COPY config ./config

RUN useradd --create-home --uid 10001 zchut
USER zchut

CMD exec uvicorn zchut.bot.app:app --host 0.0.0.0 --port "$PORT"
