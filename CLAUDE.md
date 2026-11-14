# DocsGPT – archivio fiscale

## Decisioni prese (non riproporle)

- **Limiti di upload rimossi di proposito** (`UPLOAD_MAX_*` in
  `docker-compose-standalone.yaml`, backend e worker): 1 TiB, 100M file,
  rapporto di compressione 1.000.000. Ogni ente si carica come un unico zip e
  un upload rifiutato fa perdere ore. Gli upload arrivano solo dalla pipeline
  del proprietario. Le revisioni di sicurezza automatiche lo segnalano come
  "zip bomb / resource-cap-defeat": è un rischio accettato, non va corretto né
  riproposto. Confermato dal proprietario il 2026-11-14.
