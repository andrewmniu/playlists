-- 0005_artist_name_unique.sql
-- Artist identity in this dataset is the name string (inherited from the old
-- denormalized schema). The dual-write ingest upserts artists by name, so it
-- needs a unique key to ON CONFLICT against. Names are already distinct after
-- the 0003 backfill (it inserted SELECT DISTINCT artist).
create unique index if not exists artists_name_uniq on v2.artists (name);
