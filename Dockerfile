FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Default config shipped in the image (no profile.yaml, no secrets). The entrypoint copies
# anything missing into the persistent volume on first start.
COPY config /app/config-defaults
RUN rm -f /app/config-defaults/profile.yaml
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Everything mutable (database, resumes, profile, .env) lives on the volume mounted at /vol.
ENV JOB_SEARCH_AGENT_HOME=/vol
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
