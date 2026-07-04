SELECT text_sha256, vector FROM embedding_cache WHERE model = ? AND text_sha256 IN ({0})
