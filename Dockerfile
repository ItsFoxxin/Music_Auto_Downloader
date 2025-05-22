FROM python:3.13-slim

WORKDIR /app/music_auto_downloader

COPY . .

RUN chmod +x /app/music_auto_downloader/yt-dlp

RUN apt update -y && apt install -y ffmpeg && apt clean && rm -rf /var/lib/apt/lists/*

CMD ["python", "Music_Auto_Downloader.py"]