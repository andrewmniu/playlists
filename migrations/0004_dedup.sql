-- 0004_dedup.sql
-- Collapse regional-duplicate rows in v2 now that enrichment populated the
-- identity keys (album.spotify_id, track.isrc). After this the surrogate id is
-- truly canonical, so the pipeline can drop its runtime MIN(id) canonicalization.
-- Run 0003 + enrichment first. Refresh artist_daily_plays after (done separately,
-- outside the txn, since REFRESH CONCURRENTLY can't run in a transaction).

begin;

-- ============ ALBUMS: merge rows sharing a spotify_id ============
create temp table album_canon on commit drop as
select id as dup_id, min(id) over (partition by spotify_id) as canon_id
from v2.albums
where spotify_id is not null;

update v2.tracks t set album_id = ac.canon_id
from album_canon ac
where t.album_id = ac.dup_id and ac.dup_id <> ac.canon_id;

delete from v2.albums a using album_canon ac
where a.id = ac.dup_id and ac.dup_id <> ac.canon_id;

-- uniqueness holds again now that dupes are gone
alter table v2.albums add constraint albums_spotify_id_key unique (spotify_id);

-- ============ TRACKS: merge rows sharing a non-empty isrc ============
create temp table track_canon on commit drop as
select id as dup_id, min(id) over (partition by isrc) as canon_id
from v2.tracks
where isrc is not null and isrc <> '';

-- repoint plays onto the canonical track (history PK is user_id+played_at, unaffected)
update v2.history h set track_id = tc.canon_id
from track_canon tc
where h.track_id = tc.dup_id and tc.dup_id <> tc.canon_id;

-- canonical keeps its own track_artists; drop the redundant dup ones
delete from v2.track_artists ta using track_canon tc
where ta.track_id = tc.dup_id and tc.dup_id <> tc.canon_id;

delete from v2.tracks t using track_canon tc
where t.id = tc.dup_id and tc.dup_id <> tc.canon_id;

-- enforce isrc uniqueness going forward (allow multiple '' = unknown-to-Spotify)
create unique index tracks_isrc_uniq on v2.tracks (isrc) where isrc <> '';

commit;
