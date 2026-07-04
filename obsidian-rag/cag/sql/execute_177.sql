SELECT content, content_hash, source_hash, config_version, model_version, ttl_seconds, created_at, expires_at, metadata_json FROM packs WHERE pack_type = ? AND scope = ?
