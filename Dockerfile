FROM python:3.12-slim

LABEL org.opencontainers.image.title="sinotrack-mqtt-bridge"
LABEL org.opencontainers.image.description="H02/Sinotrack GPS tracker → MQTT bridge"
LABEL org.opencontainers.image.source="https://github.com/laudetechnologies/sinotrack-mqtt-bridge"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config/config.yaml.example ./config/config.yaml.example

# Default config — override with a bind mount or env vars
RUN cp config/config.yaml.example config/config.yaml

ENV PYTHONUNBUFFERED=1

EXPOSE 8090/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import socket; s=socket.socket(); s.connect(('localhost', 8090)); s.close()" || exit 1

CMD ["python", "src/main.py", "--config", "config/config.yaml"]
