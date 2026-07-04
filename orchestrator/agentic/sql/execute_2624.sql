INSERT INTO agentic_runtime_flags (key, value_json, updated_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
