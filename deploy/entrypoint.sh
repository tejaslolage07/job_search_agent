#!/bin/sh
set -e
mkdir -p /vol/config /vol/data
cp -n /app/config-defaults/* /vol/config/ 2>/dev/null || true
exec uvicorn job_search_agent.web.hosted:app --host 0.0.0.0 --port 8080 --log-level warning --proxy-headers --forwarded-allow-ips="*"
