# Lightweight Python image; browsers are provided by Browserless (remote), so no Playwright browsers needed here.
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates fonts-dejavu \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app.py ./

# Render (and similar) provide PORT
ENV PORT=8000

CMD ["python", "app.py"]
