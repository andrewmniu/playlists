import mysql.connector
from datetime import date, datetime, timedelta
from spotify_client import SpotifyAPI
from random import randint
import os 
from dotenv import load_dotenv
load_dotenv()

today = date.today()
first = datetime.strptime('2014-07-10', '%Y-%m-%d').date()
diff = today - first
days = randint(0, diff.days)
day = first + timedelta(days=days)

track_uris = []

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

while len(track_uris) == 0:
    query = (f'''SELECT tracks.id FROM history
    JOIN tracks ON history.track_id=tracks.id
    WHERE DATE(played_at)="{day}"
    ORDER BY played_at;''')

    cursor.execute(query)
    track_uris = [track[0] for track in list(cursor)]

if len(track_uris) > 0:
    api = SpotifyAPI()
    playlist_id = "1FnYUsH4Hlp2BAjxLYj2WJ"
    name = "random"
    description = f"what i listened to on {day.strftime('%b %-d, %Y')}"
    api.delete_all_tracks(playlist_id)
    api.add_tracks_to_playlist(playlist_id,track_uris)
    api.update_playlist_details(playlist_id, name,description)

cursor.close()
db.close()