FROM python:3.13-slim

WORKDIR /app/music_auto_downloader_frontend

COPY . .

RUN apt update -y && apt clean && rm -rf /var/lib/apt/lists/*

RUN pip install -r requirements.txt

CMD ["python", "app.py"]