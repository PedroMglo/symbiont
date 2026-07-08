INSERT OR REPLACE INTO storage_idempotency_keys
                (scope, idempotency_key, payload_hash, status, object_id, response_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
