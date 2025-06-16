import mysql.connector
from datetime import date, datetime
from dateutil.relativedelta import relativedelta
from spotify_client import SpotifyAPI
import os 
from dotenv import load_dotenv
load_dotenv()

today = date.today()
year_ago = today - relativedelta(years=1)

# Get database credentials from environment variables
db_host = os.getenv("DB_HOST")
db_user = os.getenv("DB_USER")
db_password = os.getenv("DB_PASSWORD")
db_name = os.getenv("DB_DATABASE")

db = mysql.connector.connect(
    host=db_host,
    user=db_user,
    password=db_password,
    database=db_name
)
cursor = db.cursor()

query = (f'''SELECT tracks.id FROM history
JOIN tracks ON history.track_id=tracks.id
WHERE DATE(played_at)="{year_ago}"
ORDER BY played_at;''')

cursor.execute(query)
track_uris = [track[0] for track in list(cursor)]

if len(track_uris) > 0:
    api = SpotifyAPI()
    playlist_id = "7GsK6cpQmiiABLqe7O2xZY"
    name = "yesteryear"
    description = f"what i listened to on {year_ago.strftime('%b %-d, %Y')}"
    api.delete_all_tracks(playlist_id)
    api.add_tracks_to_playlist(playlist_id,track_uris)
    api.update_playlist_details(playlist_id, name,description)

cursor.close()
db.close()