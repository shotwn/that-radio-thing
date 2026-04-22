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

# Rotating log dir; bind-mounted in docker-compose so the files persist
# across container rebuilds and are directly readable from the host.
RUN mkdir -p /app/logs

EXPOSE 33408

CMD ["python", "-u", "main.py"]
