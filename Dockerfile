FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer fontconfig \
    fonts-liberation fonts-dejavu-core fonts-stix \
    fonts-crosextra-carlito fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/*
ENV HOME=/tmp
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
ENV PYTHONUNBUFFERED=1
CMD exec gunicorn --bind :$PORT --workers 1 --threads 2 --timeout 600 main:app
