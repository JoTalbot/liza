#!/bin/bash
# Старт браузерного стека: Xvfb + fluxbox + x11vnc + noVNC(websockify) + Chromium (CDP)
set -e

mkdir -p /config/profile /tmp/xdg /run/dbus
export XDG_RUNTIME_DIR=/tmp/xdg

# чистим stale-локи профиля (после неаккуратного kill контейнера Chromium не стартует)
rm -f /config/profile/SingletonLock \
      /config/profile/SingletonCookie \
      /config/profile/SingletonSocket

echo "=== Xvfb (${RESOLUTION}) ==="
Xvfb :0 -screen 0 ${RESOLUTION}x24 -ac +extension GLX +render -noreset &

echo "=== fluxbox ==="
fluxbox &

echo "=== x11vnc (:5900) ==="
X11VNC_OPTS="-display :0 -forever -shared -rfbport 5900"
if [ -n "$VNC_PASSWORD" ]; then
  echo "$VNC_PASSWORD" > /tmp/vncpass
  chmod 600 /tmp/vncpass
  X11VNC_OPTS="-display :0 -forever -shared -rfbauth /tmp/vncpass -rfbport 5900"
else
  X11VNC_OPTS="-display :0 -forever -shared -nopw -rfbport 5900"
fi
x11vnc $X11VNC_OPTS &

echo "=== noVNC (websockify :6080 -> :5900) ==="
websockify --web /usr/share/novnc 6080 localhost:5900 &

run_chromium() {
  echo "=== Chromium (CDP :9223 loopback, START_URL=${START_URL}) ==="
  # С Chromium M113+ --remote-debugging-address=0.0.0.0 игнорируется и CDP
  # слушает только 127.0.0.1. Поэтому Chromium стартует на внутреннем порту 9223,
  # а снаружи порт 9222 открывает socat (0.0.0.0:9222 -> 127.0.0.1:9223).
  chromium \
    --no-sandbox --disable-gpu --disable-dev-shm-usage \
    --remote-debugging-port=9223 \
    --remote-allow-origins=* \
    --user-data-dir=/config/profile --no-first-run --no-default-browser-check \
    --window-size=1600,1000 --force-device-scale-factor=1 \
    "${START_URL}" &
  CHROME_PID=$!
}

# CDP-проброс наружу: socat слушает 0.0.0.0:9222 (host + bot) -> 127.0.0.1:9223
echo "=== socat CDP proxy :9222 -> 127.0.0.1:9223 ==="
socat TCP-LISTEN:9222,fork,reuseaddr TCP:127.0.0.1:9223 &
SOCAT_PID=$!

graceful_shutdown() {
  echo "$(date -u +%FT%TZ) SIGTERM: корректно закрываю Chromium (куки сохраняются)..."
  # сперва аккуратно останавливаем Chromium — он флашит cookies/сессии на диск
  if [ -n "$CHROME_PID" ] && kill -0 "$CHROME_PID" 2>/dev/null; then
    kill -TERM "$CHROME_PID"
    for i in $(seq 1 15); do
      kill -0 "$CHROME_PID" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$CHROME_PID" 2>/dev/null || true
  fi
  [ -n "$SOCAT_PID" ] && kill -TERM "$SOCAT_PID" 2>/dev/null || true
  echo "$(date -u +%FT%TZ) Завершение браузерного контейнера"
  exit 0
}
trap graceful_shutdown TERM INT

run_chromium

# Watchdog: если Chromium падает — поднимаем заново
while true; do
  sleep 20
  if ! pgrep -x chromium > /dev/null; then
    echo "$(date -u +%FT%TZ) Chromium упал — перезапускаю" >> /var/log/liza-browser.log
    run_chromium
  fi
done
