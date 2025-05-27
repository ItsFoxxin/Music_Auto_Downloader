import subprocess
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/download', methods=['POST'])
def handler():
    #take the json sent from frontend and breaks it into vars
    download_request = request.json
    song_name = download_request.get('song_name')
    song_artist = download_request.get('song_artist')
    
    print(f"Incoming request: {song_name} by {song_artist}")
    
    #build search query for song and use it to send command to system
    #command grabs top 5 results, less then 5 mins, perfer topic channels or official audios as well as highest audio quality
    search_query = f"ytsearch5:{song_artist} {song_name} audio"
    
    command = [
        "/app/music_auto_downloader_backend/yt-dlp",
        "--no-playlist",
        "--audio-quality", "0"
        "--extract-audio",
        "--prefer-free-formats",
        "--no-warnings",
        "--match-filter", "duration < 600",
        "--match-title", "(?i).*official audio|topic|lyric|lyrics|official lyrics|lyric video.*",
        "-x",
        "--audio-format", "mp3",
        "-o", "/app/music_auto_downloader_backend/downloads/%(title)s.%(ext)s",
        search_query
    ]

    try:
        #run command and if song is successfully downloaded
        subprocess.run(command, check=True)
        download_request_successful = {
            "status": "ok", "message": f"Successfully downloaded {song_name} by {song_artist}!"
        }
        print(f"{song_name} by {song_artist} downloaded successfully!")
        return jsonify(download_request_successful), 200
    
    #if yt-dlp runs into any errors return
    except subprocess.CalledProcessError as e:
        download_request_failed = {
            "status": "error", "message": "Download failed!", "error_message": str(e)
        }
        print("Download failed!")
        print(e)
        return jsonify(download_request_failed), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
