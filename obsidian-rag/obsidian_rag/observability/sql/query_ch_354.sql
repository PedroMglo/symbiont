SELECT cpu_percent, ram_percent, ram_available_gb, swap_percent,
               disk_free_gb, vram_used_gb, vram_percent, active_ingest
        FROM rag_resource_samples
        ORDER BY timestamp DESC LIMIT 1
