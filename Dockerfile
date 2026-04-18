FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      libjpeg62-turbo libpng16-16 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY make_stickers.py local_webhook.py ./

ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 5000

CMD ["python", "local_webhook.py", "--host", "0.0.0.0", "--port", "5000"]
