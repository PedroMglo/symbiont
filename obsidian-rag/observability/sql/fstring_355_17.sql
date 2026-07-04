SELECT
            toStartOfFiveMinutes(timestamp) as time,
            round(avg(cpu_percent), 1) as cpu,
            round(avg(ram_percent), 1) as ram,
            round(avg(swap_percent), 1) as swap,
            round(avg(vram_percent), 1) as vram
        FROM rag_resource_samples
        WHERE timestamp > now() - INTERVAL {0} HOUR
        GROUP BY time ORDER BY time
