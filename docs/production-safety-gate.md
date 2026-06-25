# Production Safety Gate

Commit `756901f` remains `NOT SAFE` until every row in this matrix is green.

| Gate | Required Evidence | Blocking Findings |
| --- | --- | --- |
| Remote submit exactly-once | Accepted-response-loss provider test shows one outbound POST across repeated ticks and restarts | F-06, C-01 |
| Remote stale-worker fencing | Actual poll path stale worker cannot write query state, attempt outcome, classification event, live pointer, batch item status, or task DONE | F-07, C-02 |
| Ingest side-effect fencing | Mid-chunk reclaim race leaves usage, budget, remote task, and item transitions exactly once | H-01 |
| Safe backup defaults | Empty, current-directory, root, backup-parent, and symlink-to-root artifact paths fail before archive creation | H-02 |
| Durable upload staging | Web/API routes persist raw upload artifact and return before parsing rows or creating `BatchItem` rows | H-04 |
| Provider auth stop | 401/403 block all workers until explicit operator reset | H-03 |
| Remote backpressure | Production requires positive per-principal budget and global queue high-water gates | H-05 |
| Runtime secret logs | Docker Compose traffic produces zero plaintext sentinel secrets in app, proxy, worker, and ngrok-agent logs. If ngrok-agent is not run, ngrok public readiness is not passed. | C-03, M-01 |
| Runtime restore | Fresh DB and fresh volumes restore DB, media, export, source artifacts, official-import source bytes, and an HTTP export download | H-02, M-01, F-20 |
| Release workflow | CI runs lint, typecheck, migrations, PostgreSQL race tests, Docker image scan, proxy log grep, and restore smoke | M-01 |
