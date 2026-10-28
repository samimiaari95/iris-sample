FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-sentinel1.txt .
RUN pip install --no-cache-dir -r requirements-sentinel1.txt

COPY app/satellite ./app/satellite
COPY scripts/create_sentinel1_demo.py ./scripts/create_sentinel1_demo.py

CMD ["python", "-m", "app.satellite.soil_moisture", "--help"]
