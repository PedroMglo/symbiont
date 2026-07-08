INSERT INTO files (
                       source_id, source_type, path, repo, mtime, size, sha256,
                       status, chunk_count, config_version, last_indexed_at
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(source_id, path) DO UPDATE SET
                     source_type = excluded.source_type,
                     repo = excluded.repo,
                     mtime = excluded.mtime,
                     size = excluded.size,
                     sha256 = excluded.sha256,
                     status = excluded.status,
                     chunk_count = excluded.chunk_count,
                     config_version = excluded.config_version,
                     last_indexed_at = datetime('now')
