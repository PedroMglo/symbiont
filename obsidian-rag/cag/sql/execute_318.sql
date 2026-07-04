SELECT response, created_at, ttl_seconds, context_hash, model FROM response_cache WHERE query_hash = ?
