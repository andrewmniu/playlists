import mysql.connector
from spotify_client import SpotifyAPI
from datetime import datetime
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Database connection configuration from .env
DB_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'database': os.getenv('DB_DATABASE')
}

# Spotify playlist ID to update
PLAYLIST_ID = "2eImE8S0JZBnHZwCafDtII"

def get_frequently_played_tracks():
    """Retrieve canonical tracks played at least 10 times, with most recent first"""
    query = """
    WITH 
-- First identify canonical track IDs (earliest ID for each unique track)
canonical_tracks AS (
  SELECT 
    name,
    album,
    artist,
    MIN(id) as canonical_id
  FROM tracks
  GROUP BY name, album, artist
),

-- Get all plays mapped to their canonical track ID
canonical_listens AS (
  SELECT 
    ct.canonical_id,
    ct.name,
    ct.album,
    ct.artist,
    h.played_at
  FROM history h
  JOIN tracks t ON h.track_id = t.id
  JOIN canonical_tracks ct ON t.name = ct.name 
    AND t.album = ct.album 
    AND t.artist = ct.artist
),

-- Rank each listen chronologically per track
ranked_listens AS (
  SELECT 
    canonical_id as id,
    name,
    artist,
    album,
    played_at,
    ROW_NUMBER() OVER (PARTITION BY canonical_id ORDER BY played_at) as listen_number
  FROM canonical_listens
),

-- Filter to only tracks with ≥10 plays
track_play_counts AS (
  SELECT 
    id,
    name,
    artist,
    album,
    COUNT(*) as total_plays
  FROM ranked_listens
  GROUP BY id, name, artist, album
  HAVING COUNT(*) >= 10
)

-- Final result with 5th play time
SELECT 
  r.id,
  r.name,
  r.artist,
  r.album,
  pc.total_plays,
  MAX(CASE WHEN r.listen_number = 5 THEN r.played_at END) as fifth_play_time
FROM ranked_listens r
JOIN track_play_counts pc ON r.id = pc.id
GROUP BY r.id, r.name, r.artist, r.album, pc.total_plays
ORDER BY fifth_play_time DESC;
    """
    
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    cursor.execute(query)
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    
    return results

def update_spotify_playlist(tracks):
    """Update the specified playlist by clearing existing tracks and adding new ones"""
    spotify = SpotifyAPI()
    
    # Clear existing tracks from playlist
    print("Clearing existing tracks from playlist...")
    deleted_tracks = spotify.delete_all_tracks(PLAYLIST_ID)
    print(f"Removed {len(deleted_tracks)} tracks from playlist")
    
    # Add new tracks to playlist (most recent first)
    print(f"Adding {len(tracks)} tracks to playlist (most recent first)...")
    track_ids = [track['id'] for track in tracks]
    spotify.add_tracks_to_playlist(PLAYLIST_ID, track_ids)
    
    return PLAYLIST_ID

def main():
    try:
        # Get tracks from database
        print("Querying database for frequently played tracks...")
        tracks = get_frequently_played_tracks()
        print(f"Found {len(tracks)} tracks played at least 10 times")
        
        if not tracks:
            print("No tracks found matching criteria")
            return
        
        # Update Spotify playlist
        playlist_id = update_spotify_playlist(tracks)
        print(f"Successfully updated playlist {playlist_id}")
        
    except Exception as e:
        print(f"Error: {str(e)}")

if __name__ == "__main__":
    main()