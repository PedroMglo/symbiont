INSERT OR IGNORE INTO agentic_state_snapshots (
                    id, task_id, trace_id, state_hash, previous_state_hash,
                    state_json, source_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
