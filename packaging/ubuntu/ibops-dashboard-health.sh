#!/bin/sh
# A running-but-hung dashboard (it answered nothing this morning) is not
# caught by Restart=always, which only sees a process that exited. Three
# failed health checks in a row (3 minutes) restart it.
F=/run/ibops-dashboard-health.fails
if curl -fsS -m 10 -o /dev/null http://127.0.0.1:8000/healthz; then
  echo 0 > "$F"; exit 0
fi
n=$(( $(cat "$F" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$F"
logger -t ibops-health "dashboard health check failed ($n of 3)"
if [ "$n" -ge 3 ]; then
  logger -t ibops-health "dashboard not answering for 3 minutes -- restarting ibops-dashboard"
  systemctl restart ibops-dashboard
  echo 0 > "$F"
fi
