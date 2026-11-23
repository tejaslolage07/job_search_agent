#!/bin/sh
set -e
mkdir -p /vol/config /vol/data
cp -n /app/config-defaults/* /vol/config/ 2>/dev/null || true
exec job_search_agent serve --host 0.0.0.0 --port 8080
