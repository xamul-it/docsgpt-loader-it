-- Colonna tsvector salvata per la ricerca per parole chiave in italiano.
-- Senza, search_service ricalcola to_tsvector(text) per ogni frammento da
-- ordinare: sulle sorgenti grandi la query rilassata (OR) supera il timeout
-- di 8 s (misurato 8,1 s contro 0,36 s con la colonna) e restituisce zero
-- risultati senza segnalarlo.
--
-- Uso:  docker exec -i docsgpt-pgvector-1 psql -U docsgpt -d docsgpt_vectors -v ON_ERROR_STOP=1 < sql/2026-11-09_tsv_it.sql
-- Poi:  docker restart docsgpt-backend-1   (carica patches/search_service.py)
-- Annullare:  ALTER TABLE documents DROP COLUMN tsv_it;  (l'indice cade con la colonna)
--
-- L'ALTER riscrive la tabella (circa 650 mila righe, qualche minuto) tenendo
-- un lock esclusivo: le ricerche restano in attesa fino alla fine. Meglio
-- quando non ci sono caricamenti in corso.

\timing on
SET lock_timeout = '10s';
SET statement_timeout = '30min';
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS tsv_it tsvector
    GENERATED ALWAYS AS (to_tsvector('italian'::regconfig, text)) STORED;

-- Fuori transazione e senza bloccare scritture.
CREATE INDEX CONCURRENTLY IF NOT EXISTS documents_tsv_it_idx ON documents USING gin (tsv_it);
ANALYZE documents;
