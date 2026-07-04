INSERT INTO response_cache
                   (query_hash, response, context_hash, model, config_version,
                    created_at, ttl_seconds)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(query_hash) DO UPDATE SET
                     response = excluded.response,
                     context_hash = excluded.context_hash,
                     model = excluded.model,
                     config_version = excluded.config_version,
                     created_at = excluded.created_at,
                     ttl_seconds = excluded.ttl_seconds
