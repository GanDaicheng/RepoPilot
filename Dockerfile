FROM python:3.12-slim

RUN groupadd --gid 10001 repopilot \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin repopilot \
    && python -m pip install --no-cache-dir "pytest>=8.3,<10"

WORKDIR /workspace
USER 10001:10001
