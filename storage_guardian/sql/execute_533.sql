SELECT scope, idempotency_key, payload_hash, status, object_id, response_json, created_at, expires_at
                FROM storage_idempotency_keys
                WHERE scope = ? AND idempotency_key = ?
