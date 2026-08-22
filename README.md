# Our List — a shared to-do app for two (or more)

No accounts, no email, no subscriptions. Just a shared board with as many
lists as you want (Shopping, Pet stuff, Holiday, Food list — add more any
time), each person picks a name once on their own phone, and everyone sees
updates within a few seconds.

This is self-hosted, and multi-household: one running copy can host any
number of separate households, each fully private from the others. Signing
up (from inside the app itself, no email needed) generates a private login
code — that code is both a household's identity and its password. Share
that code with your own household; nobody else's household can see it or
your data, and nothing phones home.

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
repo to set everything up, including a persistent disk for your data — no
password or other setup to configure at deploy time, since households sign
up from inside the app itself. This costs about **$7.25/month** (Render's
Starter plan plus the disk) billed to *your own* Render account — it's never
billed to whoever built this app, each person who deploys pays for their own.

**Railway** works too, just without a single-click button yet: go to
[railway.app](https://railway.app) → New Project → Deploy from GitHub repo →
pick `millimillimillimilli/our-list`. Railway auto-detects the `Dockerfile`.
After the first deploy, add a Volume (Settings → Volumes) mounted at
`/data` — without that step your data won't survive a redeploy.

Whichever route, once it's deployed you'll get a URL — open that on your
phone the same way described below ("Installing it as an app on your phone"),
and create a household straight from the app's own login screen.

## Run it yourself (needs Docker + Docker Compose)

```bash
cd sharedtasks
docker compose up -d --build
```

Then open `http://<your-machine's-address>:8123` on any phone on the same
network. On your Mac mini, put it behind your existing Cloudflare Tunnel to
reach it from anywhere, the same way you've done with Nextcloud.

First person to open it creates a household straight from the login
screen — that generates a private code, which is both your household's
identity and its password. Save it somewhere safe: there's no email or
account recovery, so it's the only way back in. Everyone in your household
uses that same code to log in; after that, each person just picks a name
once on their own phone, remembered on that device only.

The same running app happily hosts more than one household at once — anyone
else can create their own separate household on the same URL, and none of
you will ever see each other's lists.

## Installing it as an app on your phone

Once it's running, open the URL on your phone in Safari (iPhone) or Chrome
(Android):

- **iPhone:** tap Share → "Add to Home Screen"
- **Android:** Chrome will usually prompt "Add to Home screen" automatically,
  or use the menu → "Install app"

It launches full-screen with its own icon, no browser bar — a proper app,
just without an App Store. It also caches itself for offline viewing, so a
patchy connection won't leave you with a blank screen.

## Notifications

Updates ("Caroline added a task") show up as a scrolling activity strip
inside the app — no setup needed. There's no push-notification option in
this multi-tenant version yet: the earlier single-household version could
push to a phone via [ntfy](https://ntfy.sh), but that used one shared topic
for the whole app, which isn't safe once more than one household can use the
same running copy — one household's reminders would otherwise get pushed to
a topic another household might also be subscribed to. A proper
per-household notification setting is a reasonable future addition, just not
built yet.

## Inviting someone else

Easiest option, if you're already running a copy: just give them your app's
URL and tell them to create their own household from the login screen —
their data is completely separate from yours on the same running app, no
extra setup on your end at all.

If they'd rather have their own fully separate deployment (own server, own
uptime, nothing riding on yours), point them at the "Deploy your own copy"
section above, or hand them the whole app to run themselves:

```bash
zip -r sharedtasks.zip sharedtasks/
```

They unzip it, run `docker compose up -d --build`, and create their own
household there — a completely independent instance, nothing shared with
yours.

## What's in here

- `app/main.py` — FastAPI backend, one SQLite database + photos folder per
  household, activity log
- `app/static/index.html` — the whole frontend, one file, no build step
- `Dockerfile` / `docker-compose.yml` — packaging for Docker / self-hosting
- `render.yaml` / `railway.json` — one-click deploy configs for those platforms
- Data lives in a persistent volume/disk (`/data`), survives restarts —
  each household gets its own subfolder under `/data/households/`

## Things you can tweak easily

- **Section colours**: `SECTION_COLORS` list in `main.py`
- **Default starter lists**: the `defaults` list in `init_household_db()`
- **Polling speed**: `setInterval(fetchState, 4000)` near the bottom of
  `index.html` — drop to 2000 for snappier updates at the cost of a few
  more requests
