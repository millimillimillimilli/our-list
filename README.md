# Our List — a shared to-do app for two (or more)

No accounts, no email, no subscriptions. Just a shared board with as many
lists as you want (Shopping, Pet stuff, Holiday, Food list — add more any
time), each person picks a name once on their own phone, and everyone sees
updates within a few seconds.

This is self-hosted: each household runs their own private copy, with their
own data, on their own server (or on a cloud platform, in a couple of
clicks — see below). Nobody else's household shares your data, and nothing
phones home.

## Deploy your own copy

**Free, no card ever charged:** [Oracle Cloud's Always Free tier](ORACLE_DEPLOY.md)
gives you a real always-on server with persistent storage at zero ongoing
cost — it's more setup than a one-click button (15-20 minutes, once), but
nobody pays anything, ever. See [ORACLE_DEPLOY.md](ORACLE_DEPLOY.md) for the
full walkthrough.

**Paid, but one click:** if you'd rather not touch a terminal at all and
don't mind paying for the hosting yourself —

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/millimillimillimilli/our-list)

Click it, connect your GitHub account, and Render reads `render.yaml` in this
repo to set everything up — a persistent disk for your data, and it'll ask
you to set one thing: `APP_PASSWORD` (the password everyone in your household
will use to open the app — pick something you can share with them). This
costs about **$7.25/month** (Render's Starter plan plus the disk) billed to
*your own* Render account — it's never billed to whoever built this app,
each person who deploys pays for their own copy.

**Railway** works too, just without a single-click button yet: go to
[railway.app](https://railway.app) → New Project → Deploy from GitHub repo →
pick `millimillimillimilli/our-list`. Railway auto-detects the `Dockerfile`. After the first deploy,
go to the service's Variables tab and add `APP_PASSWORD`, then add a Volume
(Settings → Volumes) mounted at `/data` — without that step your data won't
survive a redeploy.

Whichever route, once it's deployed you'll get a URL — open that on your
phone the same way described below ("Installing it as an app on your phone").

## Run it yourself (needs Docker + Docker Compose)

```bash
cd sharedtasks
docker compose up -d --build
```

Then open `http://<your-machine's-address>:8123` on any phone on the same
network. On your Mac mini, put it behind your existing Cloudflare Tunnel to
reach it from anywhere, the same way you've done with Nextcloud.

First person to open it types their name — it's remembered on that device
only, nothing is sent anywhere else.

## Installing it as an app on your phone

Once it's running, open the URL on your phone in Safari (iPhone) or Chrome
(Android):

- **iPhone:** tap Share → "Add to Home Screen"
- **Android:** Chrome will usually prompt "Add to Home screen" automatically,
  or use the menu → "Install app"

It launches full-screen with its own icon, no browser bar — a proper app,
just without an App Store. It also caches itself for offline viewing, so a
patchy connection won't leave you with a blank screen.

## Turning on real phone notifications ("Caroline added a task")

This uses [ntfy](https://ntfy.sh) — free, open source, no Google or Apple
account tie-in beyond what the OS itself requires for push delivery.

1. Pick a private topic name nobody would guess, e.g. `our-house-8f2k`
2. Put it in `docker-compose.yml` as `NTFY_TOPIC`
3. Install the ntfy app on both phones (App Store / Play Store) and
   subscribe to that same topic name
4. `docker compose up -d --build` again

If you want zero third parties at all, you can self-host an ntfy server too
(single Docker container) and point `NTFY_SERVER` at it instead of
`https://ntfy.sh`.

Leave `NTFY_TOPIC` blank and notifications just stay inside the app as the
scrolling activity strip at the bottom — still works fine without any setup.

## Packaging it up for someone else

Point them at the "Deploy your own copy" section above if they're fine
using Render or Railway — that's the least setup for them. If they'd rather
run it on their own hardware without touching GitHub at all, the whole app
is self-contained in this folder:

```bash
zip -r sharedtasks.zip sharedtasks/
```

They unzip it, run `docker compose up -d --build`, and they have their own
independent instance with their own database — nothing shared with yours
unless you explicitly point them at the same server.

## What's in here

- `app/main.py` — FastAPI backend, SQLite storage, activity log, ntfy push
- `app/static/index.html` — the whole frontend, one file, no build step
- `Dockerfile` / `docker-compose.yml` — packaging for Docker / self-hosting
- `render.yaml` / `railway.json` — one-click deploy configs for those platforms
- Data lives in a persistent volume/disk (`/data`), survives restarts

## Things you can tweak easily

- **Section colours**: `SECTION_COLORS` list in `main.py`
- **Default starter lists**: the `defaults` list in `init_db()`
- **Polling speed**: `setInterval(fetchState, 4000)` near the bottom of
  `index.html` — drop to 2000 for snappier updates at the cost of a few
  more requests
