SELECT
            cpu_percent, ram_percent, active_ingest
        FROM rag_resource_samples
        ORDER BY timestamp DESC LIMIT 1
