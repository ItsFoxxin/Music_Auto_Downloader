import requests
from flask import Flask, render_template, request

app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def index():
    #if http request is a get request return default page
    if request.method == 'GET':
        return render_template('index.html')
    
    #if http request is a post request use response to fill vars
    if request.method == 'POST':
        song_name = request.form['song_name']
        song_artist = request.form['song_artist']
        
        #build json with vars and send to backend
        download_request = {
            "song_name": song_name,
            "song_artist": song_artist
        }
        
        download_request = requests.post(
            "http://music_auto_downloader_backend:5001/download",
            json=download_request
        )
        
        #receive response from backend
        download_status = download_request.json()
        #if 200 send success message
        if download_status["status"] == "ok":
            success_message = download_status["message"]
            return render_template("index.html", success_message=success_message)
        
        #if 500 code send error message
        if download_status["status"] == "error":
            error_message = download_status["message", "error_message"]
            return render_template("index.html", error_message=error_message)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
