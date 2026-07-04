SELECT
            toStartOfHour(timestamp) as time,
            round(avg(latency_ms), 1) as avg_ms,
            sum(batch_size) as total_texts,
            count() as batches
        FROM rag_embedding_batches
        WHERE timestamp > now() - INTERVAL {0} HOUR
        GROUP BY time ORDER BY time
