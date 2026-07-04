SELECT timestamp as time, governor_action, governor_reason,
               ram_percent, cpu_percent
        FROM rag_ingest_stages
        WHERE timestamp > now() - INTERVAL {0} DAY AND governor_action != ''
        ORDER BY time DESC LIMIT 50
