# BD Coach live call coaching service.
#
# Holds the Agora app certificate and mints RTC tokens; everything downstream
# (Whisper, LiteLLM) is reached over the internal compose network.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY vendor ./vendor
COPY static ./static

# The Agora Web SDK is fetched at build time rather than committed: it is ~1 MB
# of vendor bundle that would otherwise live in git forever. Pinned so a build
# is reproducible.
ARG AGORA_WEB_SDK_VERSION=4.24.7
RUN mkdir -p static/vendor \
  && curl -fsSL \
    "https://unpkg.com/agora-rtc-sdk-ng@${AGORA_WEB_SDK_VERSION}/AgoraRTC_N-production.js" \
    -o static/vendor/AgoraRTC_N-production.js

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
