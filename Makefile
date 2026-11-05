.PHONY: up down logs seed test test-db tunnel rebuild clean

up:            ## build and start everything on http://localhost:8080
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f app worker

seed:          ## demo past trips for the "trip from past ones" blend: make seed USER_ID=<uuid>
	@test -n "$(USER_ID)" || (echo "usage: make seed USER_ID=<uuid>"; exit 1)
	docker compose exec app python scripts/seed_history_demo.py --user-id $(USER_ID)

rebuild:       ## fresh start, keeps the database volume
	docker compose build --no-cache && docker compose up

clean:         ## stop and delete the database volume
	docker compose down -v

tunnel:        ## also start a public HTTPS tunnel for the Telegram Mini App
	docker compose --profile telegram up --build

test:          ## unit tests (no database needed)
	cd backend && python -m pytest -q

test-db:       ## + API/queue/worker tests against a throwaway Postgres
	docker run -d --rm --name ekv-test-pg -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app -e POSTGRES_DB=app -p 55432:5432 postgres:16.15-alpine
	@sleep 4
	cd backend && TEST_DATABASE_URL=postgresql+asyncpg://app:app@localhost:55432/app python -m pytest -q; \
	  status=$$?; docker stop ekv-test-pg >/dev/null; exit $$status
