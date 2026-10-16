# Imagen unica del backend de HeatSignal: la usan los servicios `ingestion` y `api` de docker-compose.yml
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY src ./src

RUN useradd --create-home --uid 10001 heatsignal
USER heatsignal

# Default: servicio de ingesta. El servicio `api` sobreescribe el comando.
CMD ["python", "-m", "heatsignal.ingestion.run"]
