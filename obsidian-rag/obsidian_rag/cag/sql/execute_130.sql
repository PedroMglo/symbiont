INSERT INTO packs
                   (pack_type, scope, content, content_hash, source_hash,
                    config_version, model_version, ttl_seconds,
                    created_at, expires_at, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pack_type, scope) DO UPDATE SET
                     content = excluded.content,
                     content_hash = excluded.content_hash,
                     source_hash = excluded.source_hash,
                     config_version = excluded.config_version,
                     model_version = excluded.model_version,
                     ttl_seconds = excluded.ttl_seconds,
                     created_at = excluded.created_at,
                     expires_at = excluded.expires_at,
                     metadata_json = excluded.metadata_json
