FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-webhook.txt .
RUN pip install --no-cache-dir -r requirements-webhook.txt

COPY . .

EXPOSE 8000

CMD uvicorn whatsapp_webhook:app --host 0.0.0.0 --port ${PORT:-8000}
