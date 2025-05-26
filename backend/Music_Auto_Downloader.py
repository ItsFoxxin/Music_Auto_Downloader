import subprocess

while True:
    print("Music Auto Downloader v1.0.0\nPython Script by: Foxxin\nUses YT-DLP (https://github.com/yt-dlp/yt-dlp)")
    song_name = input("Please enter the name of the song you are looking for: ")
    if song_name.lower() == "exit":
        break
    song_artist = input("Please enter the artist of the song you are looking for: ")
    if song_artist.lower() == "exit":
        break
  
    search_query = f"ytsearch:{song_artist} {song_name}"

    command = [
        "/app/music_auto_downloader/yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--add-metadata",
        "-o", "/app/music_auto_downloader/downloads/%(title)s.%(ext)s",
        search_query
    ]

    try:
        subprocess.run(command, check=True)
        print("Download completed successfully!")
    except subprocess.CalledProcessError as e:
        print("Download failed!")
        print(e)
