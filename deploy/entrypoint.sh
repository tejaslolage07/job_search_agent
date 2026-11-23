#!/bin/sh
set -e
mkdir -p /vol/users
exec uvicorn job_search_agent.web.hosted:app --host 0.0.0.0 --port 8080 --log-level warning --proxy-headers --forwarded-allow-ips="*"
