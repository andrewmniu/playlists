-- 0007_track_id_realignments.sql
-- Audit trail for repointing a track at its album's own Spotify id.
--
-- 0006 moved tracks onto the release that owns them, but only v2.tracks.album_id
-- changed -- spotify_id still pointed at whatever release was ingested first.
-- That column is what the playlist jobs push to Spotify, so "Going Shopping"
-- was filed under Reality Awaits in the dashboard and still went into playlists
-- as the single, with the single's cover art.
--
-- Same recording either way (the match is by isrc), so this is cosmetic for
-- playback and load-bearing for how a playlist reads. Logged because the old id
-- is not otherwise recoverable: nothing else stores it, and history references
-- tracks.id rather than the spotify id.
--
-- To reverse:
--   update v2.tracks t set spotify_id = r.old_spotify_id
--     from (select distinct on (track_id) track_id, old_spotify_id
--             from v2.track_id_realignments order by track_id, applied_at) r
--    where r.track_id = t.id;

begin;

create table if not exists v2.track_id_realignments (
    track_id        bigint      not null references v2.tracks(id) on delete cascade,
    old_spotify_id  text        not null,
    new_spotify_id  text        not null,
    album_id        bigint      not null references v2.albums(id),
    applied_at      timestamptz not null default now(),
    primary key (track_id, applied_at)
);

commit;
