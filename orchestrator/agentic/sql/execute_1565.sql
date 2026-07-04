INSERT INTO agentic_memories (
                    id, task_id, trace_id, memory_type, source, content_preview,
                    memory_json, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    content_preview = excluded.content_preview,
                    memory_json = excluded.memory_json,
                    expires_at = excluded.expires_at
