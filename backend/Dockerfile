FROM python:3.13-slim

WORKDIR /app/music_auto_downloader

COPY . .

RUN apt update -y && apt install -y ffmpeg && apt install -y wget && apt clean && rm -rf /var/lib/apt/lists/*

RUN wget https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp && chmod +x /app/music_auto_downloader/yt-dlp

RUN mkdir /app/music_auto_downloader/downloads

CMD ["python", "Music_Auto_Downloader.py"]