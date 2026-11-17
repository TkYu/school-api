-- Who computes a batch's vectors: the worker (default, small updates) or a faster machine outside, which fetches the
-- pending texts and sends the vectors back (GET/PUT /v1/admin/embeddings); the worker then only parses and chunks.
ALTER TABLE batches ADD COLUMN embed text NOT NULL DEFAULT 'worker' CHECK (embed IN ('worker', 'remote'));
