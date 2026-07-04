INSERT INTO agentic_ai_events (
                    id, task_id, trace_id, producer, event_type, severity,
                    event_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    event_json = excluded.event_json
