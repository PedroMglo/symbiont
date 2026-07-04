INSERT INTO agentic_tasks (
                    id, goal, mode, status, priority, session_id, user_id_hash,
                    trace_id, source, created_at, updated_at, budget_json,
                    result_json, error_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
