# Deploying MuleTrace

## The constraint, first

**Netlify cannot host the backend.** Two independent reasons:

1. Netlify Functions run JavaScript/TypeScript and Go. Python is not a supported
   runtime, and the backend is FastAPI.
2. Even with a Python runtime it would not fit. The app keeps live `networkx`
   graphs and fitted `scikit-learn` models in an in-process LRU pool; standing up
   one workspace costs ~1.8 s of CPU. A stateless, short-lived function cannot
   hold that between requests, so every call would rebuild the dataset.

So the deployment is split:

```
Netlify  ──  static frontend (landing, console, login, assets)
   │
   └── /api/*  proxied to  ──  Python host (Render / Railway / Fly)  ── FastAPI
```

The proxy matters beyond convenience: it makes `/api/*` **same-origin**, so the
session cookies that carry each visitor's dataset keep working. Pointing the
frontend straight at a different origin would need CORS *and* `SameSite=None`
cookies, and would silently break per-session datasets until both were changed.
Use the proxy.

---

## 1. Backend — Render (free tier)

`render.yaml` is committed. Either connect the repo in the Render dashboard and
it will be picked up, or create a Web Service manually with:

- **Build:** `pip install -r requirements.txt`
- **Start:** `uvicorn app:app --app-dir backend --host 0.0.0.0 --port $PORT`
- **Health check:** `/api/health`
- **Env:** `MULETRACE_AUTH=on` — password + TOTP. `MULETRACE_POSTGRES_URL`
  must be the database's **External** URL: the free tiers provision in Render's
  default region regardless of `region:`, and internal `dpg-…` hostnames do not
  resolve across regions.
- **Optional:** `MULETRACE_REDIS_URL` pointing at a managed Redis, for durable
  sessions and shared dataset caching. Without it the in-process store is used
  and `/api/health` says so.

Note the free tier sleeps when idle; the first request after a sleep pays both
the cold start and a ~1.8 s workspace build. For a live demo, wake it first.

`Procfile` is also committed, so Railway and Fly work the same way.

---

## 2. Frontend — Netlify

`netlify.toml` is committed and already points at the live backend. Netlify
hosts the landing page only; `/console` and `/login` 302 to the Render service,
which serves the console and the API from one origin so the session cookie
works. There is no `/api/*` proxy, and `frontend/console.html` stays as an
unlinked read-only snapshot.

If the backend hostname ever changes, update it in three places:

- `netlify.toml` — the `/console` and `/login` redirects
- `frontend/index.html` — the three "Open Investigation Console" links

Then, from the repo root:

```bash
npx netlify login
```

```bash
npx netlify deploy --dir=frontend --prod
```

The CLI is not installed globally here, so `npx` fetches it (slow the first
time). `netlify login` opens a browser for authorisation — I could not run this
step, since it needs your account.

Alternatively connect the GitHub repo in the Netlify dashboard; `netlify.toml`
sets `publish = "frontend"` and no build command, so it deploys as-is.

---

## 3. Without a backend

The landing page still works standalone. It tries the API first and falls back to
`frontend/metrics.json` — a committed snapshot of the reference dataset, so the
figures shown are real measured output rather than placeholders.

Regenerate it whenever the detection logic or reference dataset changes:

```bash
.venv/Scripts/python.exe scripts/build_metrics.py
```

The **console** genuinely needs the API — it traces, rescans and records verdicts
against a live workspace. Without a backend it will report that the API is
unreachable.

---

## What `netlify.toml` does

| Rule | Why |
|---|---|
| `publish = "frontend"` | static assets only; no build step needed |
| `/api/*` → backend | same-origin proxy, keeps session cookies working |
| `/assets/*` → `/:splat` | pages reference `/assets/…` because FastAPI mounts them there; on Netlify the same files sit at the publish root |
| `/console` → `/console.html`, `/login` → `/login.html` | clean URLs matching the FastAPI routes |
| `no-store` on `*.html` | each page load must reach the server to be issued a new dataset |
| security headers | `nosniff`, `DENY` framing, strict referrer, locked-down permissions |

---

## Verified / not verified

- **Verified:** the static frontend serves standalone and the landing page fills
  every figure from `metrics.json` with no API present (checked in a browser
  against a plain static server — zero placeholder dashes).
- **Not verified:** the Netlify build itself, the `/api/*` proxy and the
  `/assets/*` rewrite. These are standard Netlify redirect behaviour and the
  config is written accordingly, but nothing here has been run against Netlify —
  deploying needs your account. Expect the `/assets/*` rewrite to be the first
  thing to check if styles fail to load on the deployed site.
