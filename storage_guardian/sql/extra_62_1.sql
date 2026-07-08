CREATE TABLE IF NOT EXISTS stores (
                  store_id TEXT PRIMARY KEY,
                  name TEXT,
                  path TEXT,
                  owner TEXT,
                  type TEXT,
                  mode TEXT,
                  policy TEXT,
                  enabled INTEGER,
                  placement TEXT
                );
                CREATE TABLE IF NOT EXISTS files (
                  file_id TEXT PRIMARY KEY,
                  store_id TEXT,
                  relative_path TEXT,
                  absolute_path_hash TEXT,
                  extension TEXT,
                  size_bytes INTEGER,
                  modified_at REAL,
                  accessed_at REAL,
                  processed_at REAL,
                  effective_age_days REAL,
                  hash_algo TEXT,
                  content_hash TEXT,
                  detected_type TEXT,
                  lifecycle_state TEXT,
                  last_seen_at REAL
                );
                CREATE TABLE IF NOT EXISTS archives (
                  archive_id TEXT PRIMARY KEY,
                  store_id TEXT,
                  tier TEXT,
                  backend TEXT,
                  storage_target TEXT,
                  archive_path TEXT,
                  manifest_path TEXT,
                  summary_path TEXT,
                  filelist_path TEXT,
                  verify_path TEXT,
                  original_size_bytes INTEGER,
                  archive_size_bytes INTEGER,
                  reduction_ratio REAL,
                  files_count INTEGER,
                  created_at REAL,
                  verified INTEGER,
                  effective_config_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS archive_members (
                  archive_id TEXT,
                  file_id TEXT,
                  relative_path TEXT,
                  content_hash TEXT,
                  size_bytes INTEGER,
                  member_status TEXT
                );
                CREATE TABLE IF NOT EXISTS lifecycle_events (
                  event_id TEXT PRIMARY KEY,
                  timestamp REAL,
                  cycle_id TEXT,
                  event_type TEXT,
                  store_id TEXT,
                  file_id TEXT,
                  archive_id TEXT,
                  severity TEXT,
                  message TEXT,
                  metadata_json TEXT
                );
                CREATE TABLE IF NOT EXISTS restore_events (
                  restore_id TEXT PRIMARY KEY,
                  archive_id TEXT,
                  requested_by TEXT,
                  restore_root TEXT,
                  started_at REAL,
                  finished_at REAL,
                  verified INTEGER,
                  overwritten_existing_files INTEGER
                );
                CREATE TABLE IF NOT EXISTS safety_events (
                  event_id TEXT PRIMARY KEY,
                  timestamp REAL,
                  rule TEXT,
                  action TEXT,
                  blocked INTEGER,
                  reason TEXT,
                  metadata_json TEXT
                );
                CREATE TABLE IF NOT EXISTS storage_objects (
                  object_id TEXT PRIMARY KEY,
                  latest_version_id TEXT,
                  store_id TEXT,
                  created_by TEXT,
                  created_at REAL,
                  updated_at REAL,
                  purpose TEXT,
                  zone TEXT,
                  status TEXT,
                  policy TEXT,
                  current_path TEXT,
                  relative_path TEXT,
                  absolute_path_hash TEXT,
                  size_bytes INTEGER,
                  hash_algo TEXT,
                  content_hash TEXT,
                  source_file TEXT,
                  source_content_hash TEXT,
                  parent_object_id TEXT,
                  model TEXT,
                  logical_name TEXT,
                  content_type TEXT,
                  metadata_json TEXT
                );
                CREATE TABLE IF NOT EXISTS storage_control_events (
                  event_id TEXT PRIMARY KEY,
                  timestamp REAL,
                  event_type TEXT,
                  agent TEXT,
                  action TEXT,
                  allowed INTEGER,
                  reason TEXT,
                  object_id TEXT,
                  metadata_json TEXT
                );
