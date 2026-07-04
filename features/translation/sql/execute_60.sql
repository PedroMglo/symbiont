CREATE TABLE IF NOT EXISTS translation_cache (
                    key TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    payload TEXT NOT NULL
                )
