# Dockerfile
FROM python:3.12-slim

WORKDIR /app

# System deps (minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl tar \
 && rm -rf /var/lib/apt/lists/*
 
 # grpcurl (static-ish Go binary)
RUN GRPCURL_VER="1.9.1" \
 && curl -fsSL -o /tmp/grpcurl.tgz "https://github.com/fullstorydev/grpcurl/releases/download/v${GRPCURL_VER}/grpcurl_${GRPCURL_VER}_linux_x86_64.tar.gz" \
 && tar -xzf /tmp/grpcurl.tgz -C /usr/local/bin grpcurl \
 && rm -f /tmp/grpcurl.tgz \
 && grpcurl --version


COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# create data directory for DB + results
RUN mkdir -p /app/data/results

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
