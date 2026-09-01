FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:0 \
    RESOLUTION=1600x1000 \
    START_URL=https://gemini.google.com/app

# Chromium + Xvfb + fluxbox + x11vnc + noVNC (websockify) + socat (CDP-проброс)
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    xvfb \
    fluxbox \
    x11vnc \
    websockify \
    novnc \
    socat \
    procps \
    dbus \
    dbus-x11 \
    ca-certificates \
    curl \
    fonts-liberation \
    fonts-dejavu-core \
    tini \
    && rm -rf /var/lib/apt/lists/*

COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 6080 9222

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/start.sh"]
