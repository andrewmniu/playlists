-- 0006_album_editions.sql
-- Album-edition awareness, so album-listen counting can use a *true* N.
--
-- Three problems this unblocks (all one root cause: an album row's identity and
-- its track count don't match what actually gets listened to):
--
--   1. Track dedup by isrc keeps the first-ingested row (0004's MIN(id)), which
--      for a song released as a single ahead of its album is the 1-track single.
--      Those tracks never join their album, so the album's coverage never
--      completes -- e.g. Reality Awaits held 7 of 9 tracks, with "Going Shopping"
--      and "Falling out of Love" stranded on their single rows.
--   2. total_tracks comes from whichever edition Spotify reported. On a deluxe
--      that is padded with live/demo takes the user never plays (Lucinda Williams
--      (Deluxe Edition): 12 studio tracks + 20 live), N is inflated 2-3x and the
--      album scores far fewer listens than it earned.
--   3. EPs were excluded wholesale by the builder's distinct-seen floor.
--
-- core_tracks is the standard edition's length -- the denominator we actually
-- want -- and is_variant marks the padding so it can't inflate a pass either.
-- Both are derived from Spotify and rebuildable by
-- ingest/enrich_album_editions.py; nothing here is a source of truth.

begin;

-- 'album' | 'single' | 'compilation', straight from Spotify. Distinguishes a
-- 4-track EP (album_type='single') from a 4-track maxi-single -- neither
-- total_tracks nor the name can.
alter table v2.albums add column if not exists album_type text;

-- Track count of the *standard* edition: total_tracks minus live/demo/remix
-- padding, or the standard edition's own total_tracks when we can see one.
-- NULL means "no reason to think it differs" -- readers coalesce to total_tracks.
alter table v2.albums add column if not exists core_tracks int;

-- True for a track that sits past the standard edition's last song -- the live
-- and demo takes a deluxe appends. Padding, not part of the album proper, so
-- coverage passes skip it; keeping it would let bonus-disc plays satisfy an N
-- that no longer counts them.
alter table v2.tracks add column if not exists is_variant boolean not null default false;

-- Audit trail for the re-homing pass. tracks.album_id is otherwise a one-way
-- overwrite, and the pass is heuristic, so keep enough to reverse it. A track
-- can be repaired more than once, so unwind to its earliest recorded home:
--   update v2.tracks t set album_id = r.old_album_id
--     from (select distinct on (track_id) track_id, old_album_id
--             from v2.track_album_repairs order by track_id, applied_at) r
--    where r.track_id = t.id;
create table if not exists v2.track_album_repairs (
    track_id      bigint      not null references v2.tracks(id) on delete cascade,
    old_album_id  bigint      not null references v2.albums(id),
    new_album_id  bigint      not null references v2.albums(id),
    reason        text        not null,
    applied_at    timestamptz not null default now(),
    primary key (track_id, applied_at)
);

create index if not exists albums_core_tracks_idx on v2.albums (core_tracks)
    where core_tracks is not null;

commit;
