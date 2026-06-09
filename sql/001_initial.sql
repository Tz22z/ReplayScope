CREATE TYPE trace_status AS ENUM ('recording', 'succeeded', 'failed', 'cancelled');
CREATE TYPE event_kind AS ENUM (
  'environment', 'model_call', 'tool_call', 'workspace_delta', 'checkpoint', 'run_finished'
);
CREATE TYPE replay_mode AS ENUM ('recorded', 'fresh_model', 'fresh_all');

CREATE TABLE schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE traces (
  id UUID PRIMARY KEY,
  source_run_id TEXT NOT NULL,
  framework TEXT NOT NULL,
  labels JSONB NOT NULL DEFAULT '{}',
  status trace_status NOT NULL DEFAULT 'recording',
  failure_signature TEXT,
  event_count INTEGER NOT NULL DEFAULT 0,
  chain_head TEXT NOT NULL DEFAULT repeat('0', 64),
  environment_ref JSONB,
  initial_workspace_ref JSONB,
  final_workspace_ref JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE TABLE blobs (
  digest TEXT PRIMARY KEY,
  size BIGINT NOT NULL CHECK (size >= 0),
  media_type TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE trace_events (
  id UUID PRIMARY KEY,
  trace_id UUID NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK (sequence >= 0),
  kind event_kind NOT NULL,
  name TEXT NOT NULL,
  input_ref JSONB,
  output_ref JSONB,
  error_ref JSONB,
  metadata JSONB NOT NULL DEFAULT '{}',
  duration_ms DOUBLE PRECISION CHECK (duration_ms >= 0),
  cost_usd DOUBLE PRECISION CHECK (cost_usd >= 0),
  previous_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (trace_id, sequence)
);

CREATE TABLE replay_runs (
  id UUID PRIMARY KEY,
  trace_id UUID NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
  mode replay_mode NOT NULL,
  replacement_model TEXT,
  status TEXT NOT NULL CHECK (status IN ('running', 'matched', 'diverged', 'error')),
  divergence_count INTEGER NOT NULL DEFAULT 0,
  summary JSONB NOT NULL DEFAULT '{}',
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE TABLE divergences (
  id BIGSERIAL PRIMARY KEY,
  replay_id UUID NOT NULL REFERENCES replay_runs(id) ON DELETE CASCADE,
  event_sequence INTEGER NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  expected JSONB,
  actual JSONB,
  message TEXT NOT NULL
);

CREATE TABLE reduction_jobs (
  id UUID PRIMARY KEY,
  trace_id UUID NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
  mode replay_mode NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'inconclusive', 'error')),
  baseline_signature TEXT NOT NULL,
  original_event_count INTEGER NOT NULL,
  minimal_sequences JSONB,
  trials INTEGER NOT NULL DEFAULT 0,
  cache_hits INTEGER NOT NULL DEFAULT 0,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE TABLE reduction_trials (
  id BIGSERIAL PRIMARY KEY,
  job_id UUID NOT NULL REFERENCES reduction_jobs(id) ON DELETE CASCADE,
  candidate_hash TEXT NOT NULL,
  sequences JSONB NOT NULL,
  reproduces BOOLEAN NOT NULL,
  observed_signature TEXT,
  duration_ms DOUBLE PRECISION NOT NULL,
  UNIQUE (job_id, candidate_hash)
);

CREATE TABLE evaluation_runs (
  id UUID PRIMARY KEY,
  suite_name TEXT NOT NULL,
  variant JSONB NOT NULL,
  repetitions INTEGER NOT NULL CHECK (repetitions > 0),
  case_count INTEGER NOT NULL CHECK (case_count > 0),
  solve_rate DOUBLE PRECISION NOT NULL,
  score_stddev_points DOUBLE PRECISION NOT NULL,
  total_cost_usd DOUBLE PRECISION NOT NULL,
  summary JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE evaluation_observations (
  id BIGSERIAL PRIMARY KEY,
  evaluation_id UUID NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
  trace_id UUID NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
  repetition INTEGER NOT NULL,
  solved BOOLEAN NOT NULL,
  score DOUBLE PRECISION NOT NULL,
  cost_usd DOUBLE PRECISION NOT NULL,
  duration_ms DOUBLE PRECISION NOT NULL,
  replay_id UUID REFERENCES replay_runs(id),
  metadata JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX trace_events_order_idx ON trace_events (trace_id, sequence);
CREATE INDEX traces_signature_idx ON traces (failure_signature) WHERE failure_signature IS NOT NULL;
CREATE INDEX replay_runs_trace_idx ON replay_runs (trace_id, started_at DESC);
CREATE INDEX divergences_replay_idx ON divergences (replay_id, event_sequence);
CREATE INDEX reduction_trials_job_idx ON reduction_trials (job_id, id);
CREATE INDEX evaluation_observations_run_idx ON evaluation_observations (evaluation_id, repetition);

INSERT INTO schema_migrations (version) VALUES ('001');
