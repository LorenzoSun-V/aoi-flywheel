-- 0019: when each GPU machine last asked for work. A queued job waits for a worker, and nobody could tell
-- whether the machine was running at all. Every claim (asking for a job, every few seconds while idle) and
-- every lease renewal (while running one) of a worker account sets its time here; the jobs pages show a
-- machine as online when that time is recent. One row per worker account, overwritten: it is a reading of
-- the clock, not a record (nothing in the trail of a job or the record of operations depends on it).

CREATE TABLE worker_seen (
  worker_id  bigint PRIMARY KEY REFERENCES users(id),
  seen_at    timestamptz NOT NULL
);
