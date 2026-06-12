-- Faithful lift-and-shift of the RDS MySQL schema into Postgres.
-- Same shapes as the original; normalization comes in a later migration.
-- played_at kept as timestamp WITHOUT time zone to preserve exact stored values.

create table if not exists tracks (
  id     varchar(30) primary key,
  name   varchar(255),
  album  varchar(255),
  artist varchar(100)
);

create table if not exists history (
  played_at timestamp primary key,
  track_id  varchar(30)            -- no FK, mirroring MySQL (was just an index)
);
create index if not exists history_track_id_idx on history (track_id);
