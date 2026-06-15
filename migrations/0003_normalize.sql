-- 0003_normalize.sql
-- Normalized schema for the music project, built ADDITIVELY in schema `v2`
-- alongside the live raw `public.tracks` / `public.history` (which the hourly
-- pipeline keeps writing). Nothing in `public` is touched. Reversible with:
--   drop schema v2 cascade;
--
-- Deferred (not created here):
--   * connections        -- multi-user OAuth tokens; add when 2nd user arrives
--   * track_embeddings + pgvector  -- Phase 2 music-map (pgvector not installed on VPS yet)
--
-- Enrichment (album total_tracks/isrc/image_url, artist genres/image, etc.) is a
-- SEPARATE Spotify-API pass; this migration only does the STRUCTURAL backfill from
-- the existing denormalized data, so those columns start NULL.

begin;

create schema if not exists v2;
create extension if not exists pg_trgm;   -- fuzzy artist/title search

-- ============ identity ============
create table v2.users (
  id           bigint generated always as identity primary key,
  handle       text unique not null,
  display_name text,
  timezone     text not null default 'America/Los_Angeles',
  created_at   timestamptz not null default now()
);

-- ============ catalog ============
create table v2.artists (
  id         bigint generated always as identity primary key,
  spotify_id text unique,
  name       text not null,
  image_url  text,
  genres     text[] not null default '{}',
  updated_at timestamptz not null default now()
);
create index on v2.artists using gin (name gin_trgm_ops);

create table v2.albums (
  id           bigint generated always as identity primary key,
  spotify_id   text unique,
  name         text not null,
  artist_id    bigint references v2.artists(id),
  image_url    text,
  release_date date,
  total_tracks integer,                 -- N for album-play counting; filled by enrichment
  upc          text,                    -- album-level dedup key (filled by enrichment)
  updated_at   timestamptz not null default now()
);

create table v2.tracks (
  id          bigint generated always as identity primary key,
  spotify_id  text unique,
  isrc        text,
  name        text not null,
  album_id    bigint references v2.albums(id),
  duration_ms integer,
  preview_url text,
  updated_at  timestamptz not null default now()
);
create index on v2.tracks (isrc);

create table v2.track_artists (
  track_id  bigint not null references v2.tracks(id) on delete cascade,
  artist_id bigint not null references v2.artists(id) on delete cascade,
  position  smallint not null default 0,
  primary key (track_id, artist_id)
);

-- ============ listening (ground truth) ============
create table v2.history (
  user_id   bigint not null references v2.users(id) on delete cascade,
  played_at timestamptz not null,
  track_id  bigint not null references v2.tracks(id),
  source    text not null default 'spotify',
  primary key (user_id, played_at)
);
create index on v2.history (track_id);
create index on v2.history (user_id, played_at desc);

-- ============ derived: album listens (output of count_album_plays) ============
create table v2.album_listens (
  id             bigint generated always as identity primary key,
  user_id        bigint not null references v2.users(id) on delete cascade,
  album_id       bigint not null references v2.albums(id) on delete cascade,
  started_at     timestamptz not null,
  ended_at       timestamptz not null,
  distinct_count integer not null,   -- raw stats: lets FILTER params (singles cutoff,
  total_count    integer not null,   --   N floor) re-apply on read with no recompute
  n_tracks       integer not null,
  coverage       real not null
);
create index on v2.album_listens (user_id, album_id, started_at);

-- in-flight (incomplete) album pass per user/album, carried between ingest runs
create table v2.album_pass_state (
  user_id         bigint not null references v2.users(id) on delete cascade,
  album_id        bigint not null references v2.albums(id) on delete cascade,
  seen_track_ids  bigint[] not null,
  pass_started_at timestamptz not null,
  last_played_at  timestamptz not null,
  primary key (user_id, album_id)
);

-- ============ structural backfill from public.* ============
insert into v2.users (handle, display_name) values ('andrew', 'Andrew');

insert into v2.artists (name)
select distinct artist
from public.tracks
where artist is not null and artist <> '';

insert into v2.albums (name, artist_id)
select distinct t.album, a.id
from public.tracks t
join v2.artists a on a.name = t.artist
where t.album is not null and t.album <> '';

insert into v2.tracks (spotify_id, name, album_id)
select t.id, t.name, al.id
from public.tracks t
join v2.artists ar on ar.name = t.artist
join v2.albums  al on al.name = t.album and al.artist_id = ar.id;

insert into v2.track_artists (track_id, artist_id, position)
select nt.id, ar.id, 0
from public.tracks t
join v2.tracks  nt on nt.spotify_id = t.id
join v2.artists ar on ar.name = t.artist;

insert into v2.history (user_id, played_at, track_id, source)
select 1, h.played_at, nt.id, 'spotify'
from public.history h
join v2.tracks nt on nt.spotify_id = h.track_id;

-- ============ derived: artist-play rollup (daily; rolls up exactly to week+month) ============
-- Local Pacific day to match the dashboard's bucketing. Refresh concurrently after ingest.
create materialized view v2.artist_daily_plays as
select h.user_id,
       ta.artist_id,
       (h.played_at at time zone 'America/Los_Angeles')::date as day,
       count(*)::int as plays
from v2.history h
join v2.track_artists ta on ta.track_id = h.track_id
group by h.user_id, ta.artist_id, (h.played_at at time zone 'America/Los_Angeles')::date;
create unique index on v2.artist_daily_plays (user_id, artist_id, day);

commit;
