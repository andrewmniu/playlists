-- Convert history.played_at from naive Eastern local time to canonical UTC.
-- The old Lambda stored every play as America/New_York wall-clock, so
-- interpreting the naive values in that zone recovers the exact UTC instant
-- (AT TIME ZONE handles per-row DST automatically). Lossless.
ALTER TABLE history
  ALTER COLUMN played_at TYPE timestamptz
  USING played_at AT TIME ZONE 'America/New_York';
