docker compose down && docker compose up -d
docker compose logs -f scalper-prime

After the first trading day, check:

docker exec scalper-prime cat /bot/analytics/tick_velocity_report.txt
The report tells you exactly what to change and provides the config line to copy — no interpretation needed.