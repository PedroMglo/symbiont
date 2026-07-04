INSERT INTO agentic_command_sessions (
                    id, task_id, trace_id, context_profile, cwd, status,
                    created_at, updated_at, expires_at, closed_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
