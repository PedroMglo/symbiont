INSERT INTO system_resources (
                    timestamp, gpu_name, gpu_vram_total_mb, gpu_vram_used_mb,
                    gpu_vram_free_mb, gpu_utilization_pct, gpu_temperature_c, gpu_power_w,
                    ram_total_mb, ram_used_mb, ram_available_mb, ram_percent,
                    swap_total_mb, swap_used_mb,
                    cpu_count, cpu_percent,
                    ollama_models_loaded, ollama_vram_used_mb, models_loaded_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
