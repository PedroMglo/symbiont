INSERT INTO agentic_messages (
                    id, task_id, trace_id, round_id, message_type, sender,
                    recipient, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload_json = excluded.payload_json
