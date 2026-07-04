CREATE TABLE IF NOT EXISTS storage_objects (
                  object_id TEXT PRIMARY KEY,
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
                  metadata_json TEXT
                );
                CREATE TABLE IF NOT EXISTS storage_object_versions (
                  version_id TEXT PRIMARY KEY,
                  object_id TEXT,
                  store_id TEXT,
                  created_by TEXT,
                  created_at REAL,
                  zone TEXT,
                  status TEXT,
                  policy TEXT,
                  logical_name TEXT,
                  content_type TEXT,
                  current_path TEXT,
                  relative_path TEXT,
                  size_bytes INTEGER,
                  hash_algo TEXT,
                  content_hash TEXT,
                  parent_object_id TEXT,
                  metadata_json TEXT
                );
                CREATE TABLE IF NOT EXISTS storage_upload_sessions (
                  upload_id TEXT PRIMARY KEY,
                  object_id TEXT,
                  version_id TEXT,
                  store_id TEXT,
                  created_by TEXT,
                  created_at REAL,
                  updated_at REAL,
                  expires_at REAL,
                  zone TEXT,
                  status TEXT,
                  policy TEXT,
                  logical_name TEXT,
                  content_type TEXT,
                  temp_path TEXT,
                  final_path TEXT,
                  expected_size INTEGER,
                  received_size INTEGER,
                  hash_algo TEXT,
                  expected_hash TEXT,
                  metadata_json TEXT
                );
                CREATE TABLE IF NOT EXISTS storage_idempotency_keys (
                  scope TEXT,
                  idempotency_key TEXT,
                  payload_hash TEXT,
                  status TEXT,
                  object_id TEXT,
                  response_json TEXT,
                  created_at REAL,
                  expires_at REAL,
                  PRIMARY KEY (scope, idempotency_key)
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
                CREATE INDEX IF NOT EXISTS idx_storage_objects_agent_status
                  ON storage_objects(created_by, status);
                CREATE INDEX IF NOT EXISTS idx_storage_objects_store_zone
                  ON storage_objects(store_id, zone);
                CREATE INDEX IF NOT EXISTS idx_storage_versions_object
                  ON storage_object_versions(object_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_storage_upload_status
                  ON storage_upload_sessions(status, expires_at);
