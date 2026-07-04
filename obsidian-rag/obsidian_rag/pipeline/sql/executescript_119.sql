PRAGMA foreign_keys=OFF;

                DROP TABLE IF EXISTS files_v2;
                DROP TABLE IF EXISTS chunks_v2;

                CREATE TABLE files_v2 (
                    source_id       TEXT NOT NULL DEFAULT '',
                    source_type     TEXT NOT NULL DEFAULT '',
                    path            TEXT NOT NULL,
                    repo            TEXT NOT NULL,
                    mtime           REAL NOT NULL,
                    size            INTEGER NOT NULL,
                    sha256          TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'indexed',
                    chunk_count     INTEGER NOT NULL DEFAULT 0,
                    config_version  TEXT NOT NULL DEFAULT '',
                    last_indexed_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (source_id, path)
                );

                INSERT OR REPLACE INTO files_v2 (
                    source_id, source_type, path, repo, mtime, size, sha256,
                    status, chunk_count, config_version, last_indexed_at
                )
                SELECT
                    COALESCE(NULLIF(repo, ''), 'unknown') AS source_id,
                    '' AS source_type,
                    path, repo, mtime, size, sha256, status, chunk_count,
                    COALESCE(config_version, '') AS config_version,
                    last_indexed_at
                FROM files;

                CREATE TABLE chunks_v2 (
                    chunk_id      TEXT PRIMARY KEY,
                    source_id     TEXT NOT NULL DEFAULT '',
                    source_type   TEXT NOT NULL DEFAULT '',
                    file_path     TEXT NOT NULL,
                    repo          TEXT NOT NULL,
                    chunk_hash    TEXT NOT NULL,
                    vector_status TEXT NOT NULL DEFAULT 'pending',
                    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (source_id, file_path) REFERENCES files_v2(source_id, path) ON DELETE CASCADE
                );

                INSERT OR REPLACE INTO chunks_v2 (
                    chunk_id, source_id, source_type, file_path, repo,
                    chunk_hash, vector_status, created_at
                )
                SELECT
                    chunk_id,
                    COALESCE(NULLIF(repo, ''), 'unknown') AS source_id,
                    '' AS source_type,
                    file_path, repo, chunk_hash, vector_status, created_at
                FROM chunks;

                DROP TABLE chunks;
                DROP TABLE files;
                ALTER TABLE files_v2 RENAME TO files;
                ALTER TABLE chunks_v2 RENAME TO chunks;

                PRAGMA foreign_keys=ON;
