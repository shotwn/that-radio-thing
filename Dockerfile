FROM node:22-alpine AS admin-build

WORKDIR /admin
COPY admin/package.json ./
COPY admin/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY admin ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY main.py ./
COPY thatradiothing ./thatradiothing
COPY static ./static
COPY --from=admin-build /admin/dist ./static/admin

# Rotating log dir; bind-mounted in docker-compose so the files persist
# across container rebuilds and are directly readable from the host.
RUN mkdir -p /app/logs /app/data

EXPOSE 33408

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('TRT_PORT', '33408') + '/health/live', timeout=3).read()"]

CMD ["python", "-u", "main.py"]
