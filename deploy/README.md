# Deploy (novel.anlonely.me)

This project is a FastAPI app. Production setup on `anlonely.me` hosts uses:

- Docker Compose for app + reverse proxy
- Nginx + Certbot for HTTPS (the server already runs nginx on 80/443)
- Persistent `./data` volume for SQLite DB and exports
- Runtime config stays in `data/settings.json`, `data/prompts.json`, and
  `data/api_profiles.json`; the deploy script excludes them from rsync so
  prompt/settings edits survive rebuilds and redeploys.

## Prereqs on server

- DNS: `novel.anlonely.me` A/AAAA record points to the server public IP.
- Nginx already listens on 80/443; `novel-expander` binds only to `127.0.0.1:8899`.
- Docker + Docker Compose installed.

## Deploy steps (server)

1. Upload repo to server (git clone or rsync).
2. In the repo folder:

```bash
cp .env.example .env
vi .env  # set API_KEY / ADMIN_API_KEY if needed, and SITE_AUTH_PASSWORD for app login
docker compose up -d --build
```

3. Configure nginx and issue a certificate (example):

```bash
cp deploy/nginx.novel.anlonely.me.conf /etc/nginx/sites-available/novel.anlonely.me
ln -sf /etc/nginx/sites-available/novel.anlonely.me /etc/nginx/sites-enabled/novel.anlonely.me
nginx -t && systemctl reload nginx
certbot --nginx -d novel.anlonely.me
```

## Smoke tests

```bash
curl -sS -c /tmp/novel.cookies \
  -d 'username=novel' \
  --data-urlencode 'password=<password>' \
  https://novel.anlonely.me/api/login >/dev/null
curl -sS -b /tmp/novel.cookies https://novel.anlonely.me/api/health | jq .
curl -sS -b /tmp/novel.cookies -X POST https://novel.anlonely.me/api/model-test \
  -H 'content-type: application/json' \
  -d '{"model":"grok-4.20-auto","prompt":"用一句话复述：今天天气很好。"}' | jq .
```

If `SITE_AUTH_PASSWORD` is set, browser access uses `/login` and a signed
HttpOnly cookie instead of Nginx Basic Auth.
