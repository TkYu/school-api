-- Hiding one document (reversible): kept with its whole history, but neither searched nor served to Tomo.
ALTER TABLE documents ADD COLUMN hidden_at timestamptz, ADD COLUMN hidden_reason text, ADD COLUMN hidden_by text;

CREATE OR REPLACE VIEW document_status AS
  SELECT d.id AS document_id, d.source_id, d.key, cv.id AS current_version_id,
         (lf.id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM sightings s JOIN versions v ON v.id = s.version_id
            WHERE v.document_id = d.id AND s.batch_id = lf.id)) AS gone,
         (d.hidden_at IS NOT NULL) AS hidden
  FROM documents d
  LEFT JOIN current_versions cv ON cv.document_id = d.id
  LEFT JOIN LATERAL (
    SELECT b.id FROM batches b
    WHERE b.source_id = d.source_id AND b.kind = 'full' AND b.status = 'published'
    ORDER BY b.finished_at DESC NULLS LAST, b.id DESC LIMIT 1) lf ON true;
