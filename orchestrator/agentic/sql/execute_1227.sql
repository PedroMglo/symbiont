INSERT INTO agentic_decisions (
                    id, task_id, trace_id, input_state_hash, decision_status,
                    confidence, decision_json, raw_output_ref_json, valid,
                    error_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
