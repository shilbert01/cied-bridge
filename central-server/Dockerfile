# python:3.12-slim is a multi-arch image (Debian Trixie, glibc 2.41) — same
# file deploys on amd64 (dev) and arm64 (Pi 4B / Raspberry Pi).  The glibc
# version matters: gpdl is built on the Pi (Trixie) and bind-mounted into the
# container, so both must share the same glibc lineage.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ghostscript \
        openssl \
    && rm -rf /var/lib/apt/lists/*
# PCL/PJL print jobs (Abbott Merlin) require ghostpdl (gpdl binary).
# ghostpdl is not in Debian Bookworm; install manually on the host and
# bind-mount it:  volumes: ["/usr/bin/gpdl:/usr/local/bin/gpdl:ro"]

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ ./api/
COPY ui/  ./ui/
COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8443

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8443", \
     "--ssl-keyfile",  "/certs/server.key", \
     "--ssl-certfile", "/certs/server.crt", \
     "--workers", "1", \
     "--log-level", "info"]
