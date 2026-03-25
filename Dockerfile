FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY package.json ./
RUN npm install --omit=dev

COPY app ./app
COPY README.md ./README.md

RUN mkdir -p /app/output

EXPOSE 5000

CMD ["python", "-m", "app.main"]
