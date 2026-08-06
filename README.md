# 999.md apartment watcher → Telegram

Checks 999.md every hour for **new monthly-rent apartments in new blocks (Construcţii noi)** in
**Râșcani, Ciocana, Poșta Veche** priced **300–400 €/month**, and pushes each new listing to your
Telegram.

It talks to 999.md's own GraphQL endpoint — the same one their website calls — so there's no HTML
scraping, no API key and no login. Pure Python standard library, one file, no `pip install`.

---

## 1. Create the Telegram bot (2 minutes)

1. In Telegram, open **@BotFather** → send `/newbot` → pick a name and a username.
2. BotFather replies with a token like `8123456789:AAH...`. That's your `TELEGRAM_TOKEN`.
3. Open your new bot's chat and press **Start** (or send it any message) — a bot can't message you
   until you've talked to it first.
4. Get your chat id:

   ```bash
   TELEGRAM_TOKEN=8123456789:AAH... python3 bot999.py --chat-id
   ```

   It prints something like `TELEGRAM_CHAT_ID=123456789`.

## 2. First run — seed, don't spam

The very first run would otherwise alert you about every listing that already matches. Seed it:

```bash
export TELEGRAM_TOKEN=8123456789:AAH...
export TELEGRAM_CHAT_ID=123456789

python3 bot999.py --dry-run   # see what currently matches
python3 bot999.py --seed      # remember them, send nothing
```

From now on, `python3 bot999.py` only messages you about listings it has never seen.

## 3. Run it hourly

### Linux / macOS — cron

```bash
crontab -e
```

Add (adjust the path):

```cron
7 * * * * cd /home/you/bot999 && TELEGRAM_TOKEN=8123456789:AAH... TELEGRAM_CHAT_ID=123456789 /usr/bin/python3 bot999.py >> bot999.log 2>&1
```

Minute `7` rather than `0` just spreads the load off the top of the hour.

### Windows — Task Scheduler

Create a Basic Task → trigger *Daily*, then in the task's **Triggers → Edit** tick
*Repeat task every 1 hour for a duration of 1 day*. Action: *Start a program*
→ `python.exe`, arguments `bot999.py`, "Start in" = the folder. Set `TELEGRAM_TOKEN` and
`TELEGRAM_CHAT_ID` as user environment variables first (System Properties → Environment Variables),
so they aren't visible in the task definition.

### GitHub Actions — runs in the cloud, nothing on your machine

Push this folder to a **private** repo and add the workflow in `.github/workflows/watch.yml`
(included here). Then in the repo: **Settings → Secrets and variables → Actions → New repository
secret** for `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.

The workflow commits `seen.json` back to the repo after each run, which is how it remembers.
Note GitHub's scheduled runs are best-effort and often fire 5–20 minutes late.

---

## Tuning

Everything is an environment variable — no code editing needed.

| Variable | Default | What it does |
|---|---|---|
| `PRICE_MIN` / `PRICE_MAX` | `300` / `400` | Price band, EUR per month |
| `REGIONS_WATCHED` | `Rascani,Ciocana,Posta Veche` | Comma-separated, from the list below |
| `ROOMS` | *(any)* | e.g. `1,2` to only get 1- and 2-room flats |
| `MATCH_OTHER_CURRENCIES` | `1` | Also match MDL/USD listings, converted to EUR |
| `MDL_PER_EUR` / `USD_PER_EUR` | `19.4` / `1.09` | Conversion rates — refresh occasionally |
| `PAGE_SIZE` / `MAX_PAGES` | `100` / `20` | Paging through the full result set |
| `MAX_ALERTS_PER_RUN` | `15` | Flood guard |
| `STATE_FILE` | `seen.json` next to the script | Where seen ids live |

**Available regions:** Centru, Botanica, Buiucani, Rascani, Telecentru, Ciocana, Posta Veche,
Sculeni, Aeroport, BAM, Paminteni, 6 cartier, 7 cartier, 8 cartier, 9 cartier.

To also watch **secondary-market** flats, open `bot999.py` and drop the
`{"filterId": 2307, ...}` entry from `filters` in `fetch_listings()`.

---

## Notes and caveats

- **New-block filter** uses 999.md's own *Construcţii noi* attribute, which the seller sets. A
  handful of genuinely new-block flats are mis-tagged as *Secundar* by lazy posters and won't show up.
- **Bumped listings.** 999.md lets sellers "refresh" an old ad to the top. The bot dedupes by listing
  id, so a refreshed old ad never re-alerts you.
- **Full sweep, every run.** The bot pages through the entire filtered result set (~500 listings at
  the time of writing), not just the newest page. A cheap flat posted by someone who never bumps
  their ad sits deep in the list and would otherwise be invisible.
- **Price band reality check:** ~500 monthly-rent new-block flats across those three sectors, of
  which 13 were in the 300–400 € band. Expect a quiet bot — that's the point. If it's *too* quiet,
  widen `PRICE_MAX` to 450.
- **If it ever stops finding anything**, 999.md most likely changed a filter id. The ids are all in
  the config block at the top of the script; they're recoverable from a browser's network tab by
  watching the `SearchAds` POST to `https://999.md/graphql` while ticking filters on the site.
- Be polite: hourly is fine, don't drop it to every minute.
