-- Settling postponed fixtures. Bookmakers void a leg whose match is not played within a window of
-- the kickoff it was placed against (tracker.postponed_void_hours), so keep that kickoff: the
-- event's own starts_at moves when football-data.org reports a rearranged date.
ALTER TABLE tracked_bet_leg ADD COLUMN kickoff_at TIMESTAMPTZ;
UPDATE tracked_bet_leg l SET kickoff_at = e.starts_at FROM event e WHERE e.id = l.event_id;
