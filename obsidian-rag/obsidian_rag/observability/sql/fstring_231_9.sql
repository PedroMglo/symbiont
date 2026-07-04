SELECT
            multiIf(best_score < 0.3, '0.0-0.3', best_score < 0.5, '0.3-0.5',
                    best_score < 0.7, '0.5-0.7', best_score < 0.9, '0.7-0.9', '0.9-1.0') as bucket,
            count() as value
        FROM rag_retrieval
        WHERE timestamp > now() - INTERVAL {0} DAY AND best_score > 0
        GROUP BY bucket ORDER BY bucket
