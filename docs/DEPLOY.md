# Running Marketing Trends locally (Docker)

The whole product — backend API, the web UI, the SQLite database, and the
scrapers — runs as **one Docker container on your own machine**. Nothing is
hosted on a server: harvesting happens from *your* internet connection (this is
deliberate — see [ADR-0001](adr/0001-diy-scraping-under-zero-budget.md)), and the
database is a local SQLite file on your disk.

This works the same on **macOS** and **Windows**.

---

## 1. One-time setup

1. Install **[Docker Desktop](https://www.docker.com/products/docker-desktop/)**
   (macOS or Windows) and launch it. Wait until the whale icon says it's running.
2. Get this project folder onto your machine (clone the repo or copy the folder).

## 2. Start it

**The easy way (double-click):**

- **macOS:** double-click `run.command`
- **Windows:** double-click `run.bat`

**Or from a terminal**, inside the project folder:

```bash
docker compose up --build -d
```

The **first** start builds the image and downloads ~2 GB (the browser engines the
scrapers need), so it can take several minutes. Later starts are seconds.

When it's ready, open **http://localhost:8001** in your browser.

## 3. Stop / restart / update

```bash
docker compose down                 # stop
docker compose up -d                # start again (no rebuild)
git pull && docker compose up --build -d   # update to a new version
```

---

## Where your data lives

Everything mutable is stored in folders next to this file, **on your disk** — it
survives restarts, rebuilds, and `docker compose down`:

| Folder       | What's in it                                                        |
| ------------ | ------------------------------------------------------------------- |
| `data/`      | `trends.db` (the SQLite corpus) + `media/` (downloaded videos/images) |
| `profiles/`  | Per-account browser profiles / login sessions used for harvesting   |
| `secrets/`   | Platform session secrets and any `.env` — never baked into the image |

Deleting `data/` resets your local corpus. Backing up = copying these folders.

## First run is empty — fill it by harvesting

A fresh install starts with an **empty** corpus. Use the **Refresh** menu in the
UI → **Hard refresh (live)** to pull new posts from the platforms into your local
database. Media downloads in the background as you browse.

> **Starting from an existing corpus instead:** drop a prebuilt `trends.db` (and
> its `media/` folder) into `data/` *before* the first launch, and the app will
> use it as-is.

## Harvesting needs logins for some platforms

X works without login. TikTok, Instagram, and Threads generally need a logged-in
browser session to harvest reliably. Those sessions live in `profiles/` and
`secrets/`; set them up once and they persist across restarts. (See
[`docs/handoffs/robust-harvest.md`](handoffs/robust-harvest.md).)

---

## Troubleshooting

- **"Docker isn't running"** — start Docker Desktop first, then retry.
- **Port 8001 already in use** — something else is on that port. Stop it, or change
  the published port in `docker-compose.yml` (e.g. `"8080:8001"`) and open the new
  port instead.
- **A platform won't harvest** — its login session likely expired; refresh the
  session in `profiles/`/`secrets/`. One platform failing never blocks the others.
- **See what's happening** — `docker compose logs -f` streams the live logs.
- **Chromium crashes during harvest** — usually shared-memory; `shm_size` is already
  raised to 1 GB in `docker-compose.yml`, increase it further if needed.
