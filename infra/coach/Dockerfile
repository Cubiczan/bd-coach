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
# is reproducible; sha256sum -c fails the build on a CDN swap.
ARG AGORA_WEB_SDK_VERSION=4.24.7
ARG AGORA_WEB_SDK_SHA256=5feb64777698f32868bdd5301061ada4ba2bc47e17b43b7ee8138a5222dda466
RUN mkdir -p static/vendor \
  && curl -fsSL \
    "https://unpkg.com/agora-rtc-sdk-ng@${AGORA_WEB_SDK_VERSION}/AgoraRTC_N-production.js" \
    -o static/vendor/AgoraRTC_N-production.js \
  && echo "${AGORA_WEB_SDK_SHA256}  static/vendor/AgoraRTC_N-production.js" | sha256sum -c -

RUN addgroup --system coach \
  && adduser --system --ingroup coach --no-create-home --home /app coach \
  && chown -R coach:coach /app
USER coach

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
