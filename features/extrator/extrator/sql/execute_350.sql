SELECT table_id, doc_id, name, rows, columns, output_path, summary
                FROM tables WHERE doc_id = ? ORDER BY table_id
