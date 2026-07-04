UPDATE jobs
                SET status = ?, started_at = ?, completed_at = ?, error = ?,
                    outputs_json = ?, summary_json = ?
                WHERE job_id = ?
