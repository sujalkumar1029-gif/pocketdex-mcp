FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pocketdex.py .

RUN useradd --create-home --uid 10001 pocketdex \
    && mkdir -p /app/data/projects \
    && chown -R pocketdex:pocketdex /app

USER pocketdex

ENTRYPOINT ["python3", "pocketdex.py", "serve"]
