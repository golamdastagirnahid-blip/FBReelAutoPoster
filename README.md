# FBReelAutoPoster

Smart, fully automated Facebook **Reel** posting tool that runs entirely on
**GitHub Actions** (zero cost, forever — within the free tier).

- Source: a **public Google Drive folder** of `.mp4` reels.
- Metadata: **extracted from each video's MP4 tags / filename** (no AI).
- Enhancement: light **FFmpeg** color + audio normalization.
- Posting: **5–7 reels per day** at **randomized, humanized times**
  inside your local posting window.
- Dedup: state stored as JSON in the repo, committed back each run.
- 100% free: GitHub Actions + Google Drive API key (no billing required).

---

## 1. How it works

```
[Drive folder of .mp4]  ──► list ──► pick unposted ──► download
                                                      │
                                                      ▼
                                          ffprobe metadata + filename
                                                      │
                                                      ▼
                                          ffmpeg light enhance
                                                      │
                                                      ▼
                                Facebook Reels 3-phase upload (Graph API)
                                                      │
                                                      ▼
                              state/posted.json + schedule_*.json committed
```

A GitHub Actions cron runs every 30 minutes. On the first run of each
local-time day, the scheduler generates 5–7 random posting times inside
your window (default 09:00–22:00). Every later run checks whether a slot
is due now; if yes, it posts one reel; if not, it exits quickly.

Schedules are **deterministic per date + page id**, so even if state is
lost the same times would be regenerated (and already-posted videos are
still skipped via `state/posted.json`).

---

## 2. One-time setup

### 2.1 Facebook side

1. You need a **Facebook Page** (not just a personal profile).
2. Create a Meta Developer App at <https://developers.facebook.com/apps>.
   - Add the product **"Facebook Login"** (any settings — we won't use the UI).
3. In **Graph API Explorer** (<https://developers.facebook.com/tools/explorer/>):
   - Pick your app.
   - Click **Generate Access Token** with these permissions:
     `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`,
     `pages_manage_engagement`, `publish_video`.
   - Copy the resulting **short-lived user token**.
4. Convert it to a **never-expiring Page token** locally:
   ```bash
   python scripts/get_long_lived_token.py \
     --app-id YOUR_APP_ID \
     --app-secret YOUR_APP_SECRET \
     --short-user-token SHORT_USER_TOKEN \
     --page-id YOUR_PAGE_ID
   ```
   Save the printed page token — this is your `FB_PAGE_TOKEN` secret.

### 2.2 Google Drive side

The tool supports **three auth modes**. Pick whichever fits your situation.

#### Mode A — OAuth user credentials (recommended; needed for auto-archiving)

After each successful post the tool moves the source video to an
**"Already Uploaded"** archive folder, so your source folder always
shows only what's still pending. This mode acts AS YOU — no folder
sharing needed, the folders can be fully private.

> Works even if your org has the `iam.disableServiceAccountKeyCreation`
> policy enabled, because OAuth Client IDs are a different credential
> type that policy doesn't cover.

**One-time setup (~5 min):**

1. Open or create any GCP project:
   <https://console.cloud.google.com/projectcreate>
2. **Enable the Drive API**:
   <https://console.cloud.google.com/apis/library/drive.googleapis.com>
   → **Enable**.
3. **Configure the OAuth consent screen** (APIs & Services → OAuth
   consent screen):
   - User type: **External** (or **Internal** if your account is in a
     Google Workspace — Internal skips the 7-day token expiry).
   - Fill the required app name + your email; everything else can be
     left blank.
   - Under **Test users**, click **Add users** and add your own email.
     Save.
4. **Create the OAuth Client** (APIs & Services → Credentials →
   Create credentials → OAuth client ID):
   - Application type: **Desktop app**
   - Name: e.g. `fb-reel-poster`
   - Click Create, then **Download JSON** for the new client.
5. **Run the helper script locally** (one time):
   ```bash
   pip install -r requirements.txt
   python scripts/get_drive_oauth_token.py --client-secrets path/to/downloaded.json
   ```
   A browser tab opens — log in with the Google account that owns your
   Drive folders, click **Allow**. The script prints three values:
   `DRIVE_OAUTH_CLIENT_ID`, `DRIVE_OAUTH_CLIENT_SECRET`,
   `DRIVE_OAUTH_REFRESH_TOKEN`.
6. **Create the archive folder** in Drive ("Already Uploaded"). Copy
   its URL or ID for the next step.

**GitHub secrets to add:**

| Name                          | Value                                       |
| ----------------------------- | ------------------------------------------- |
| `DRIVE_FOLDER_ID`             | Source folder URL or raw ID                 |
| `DRIVE_ARCHIVE_FOLDER_ID`     | Archive folder URL or raw ID                |
| `DRIVE_OAUTH_REFRESH_TOKEN`   | From the helper script output               |
| `DRIVE_OAUTH_CLIENT_ID`       | From the helper script output               |
| `DRIVE_OAUTH_CLIENT_SECRET`   | From the helper script output               |

> **7-day token caveat (External + Testing):** When your OAuth consent
> screen User type is "External" and status is "Testing", refresh
> tokens currently expire after 7 days. To make tokens permanent:
> - **Easiest:** click **Publish App** on the consent screen. As long
>   as you stay under 100 users, no Google review is required just to
>   remove the 7-day limit for your own account.
> - **Workspace users:** change User type to **Internal**.

#### Mode B — Service Account (only if your org allows SA keys)

If the `iam.disableServiceAccountKeyCreation` policy does NOT apply to
you, this is slightly simpler:

1. Create a service account, **Keys → Add key → JSON** → download.
2. Enable Drive API.
3. Copy the `client_email` from the JSON.
4. Share both Drive folders with that email as **Editor**.
5. Add secrets: `DRIVE_SERVICE_ACCOUNT_JSON` (full JSON file contents)
   and `DRIVE_ARCHIVE_FOLDER_ID`.

#### Mode C — Keyless (simplest; no archiving)

1. Share your source folder as **"Anyone with the link → Viewer"**.
2. Set just `DRIVE_FOLDER_ID`.
3. Skip all the OAuth / SA secrets.

In this mode the source folder grows over time; dedup still prevents
double-posts via `state/posted.json`.

### 2.3 Repo secrets and variables

In **Settings → Secrets and variables → Actions**:

**Required secrets:**

| Name              | Value                                              |
| ----------------- | -------------------------------------------------- |
| `FB_PAGE_ID`      | Your Facebook Page numeric ID                      |
| `FB_PAGE_TOKEN`   | Long-lived Page access token from 2.1              |
| `DRIVE_FOLDER_ID` | Source folder URL or raw ID                        |

**Optional secrets (Mode A only):**

| Name                          | Value                                       |
| ----------------------------- | ------------------------------------------- |
| `DRIVE_ARCHIVE_FOLDER_ID`     | Archive folder URL or raw ID                |
| `DRIVE_SERVICE_ACCOUNT_JSON`  | Full contents of the service account JSON   |

**Variables** (optional — sensible defaults shown):

| Name                     | Default       | Notes                                      |
| ------------------------ | ------------- | ------------------------------------------ |
| `TIMEZONE`               | `Asia/Dhaka`  | Any IANA tz, e.g. `Asia/Singapore`         |
| `POSTS_PER_DAY_MIN`      | `5`           | Lower bound (inclusive)                    |
| `POSTS_PER_DAY_MAX`      | `7`           | Upper bound (inclusive)                    |
| `WINDOW_START_HOUR`      | `9`           | Local hour, inclusive                      |
| `WINDOW_END_HOUR`        | `22`          | Local hour, exclusive                      |
| `SLOT_TOLERANCE_MINUTES` | `20`          | Fire if a slot is due within ±this window  |
| `FB_API_VERSION`         | `v21.0`       | Graph API version                          |

### 2.4 Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<you>/FBReelAutoPoster.git
git push -u origin main
```

The workflow will start running on its cron automatically. To test
immediately, go to **Actions → Post FB Reel → Run workflow** (toggle
**Dry run** first to verify pipeline without publishing).

---

## 3. Title & hashtag generation (no AI)

Each caption is built from two things — **no AI calls, no external API**:

### Title — cleaned from the video's filename, with smart fallback

The tool:

1. Strips a known scraper / platform handle (e.g. `masstiktok_muskoluk1__`,
   `tiktok_@user__`, `ssstik.io_1700__`, `snaptik_12345_  `, …) — case
   insensitive on the platform name only, so it can't accidentally eat
   into the real title that follows.
2. Falls back to a **generic** lowercase-handle detector for unknown
   scrapers (`unknown_scraper_user__Real title` → `Real title`).
3. Drops any leftover `#tag` fragments (Drive truncates filenames at
   ~50 chars so trailing tags are unreliable).
4. If after cleaning nothing meaningful is left (e.g. the filename was
   nothing but hashtags), picks a random title from `titles.txt`.

Examples (verified against your actual 211-file folder):

| Drive filename                                                | Reel title                              |
| ------------------------------------------------------------- | --------------------------------------- |
| `masstiktok_muskoluk1__ A bunch of pink baby birds hugging  #.mp4` | `A bunch of pink baby birds hugging` |
| `tiktok_@coolguy__The 3 habits that changed my life.mp4`      | `The 3 habits that changed my life`     |
| `ssstik.io_1700__ Cute Bird Family.mp4`                       | `Cute Bird Family`                      |
| `masstiktok_muskoluk1__#bird #viral #nature.mp4`              | `The most heartwarming bird video today` *(from `titles.txt`)* |
| `My Reel.mp4`                                                 | `My Reel`                               |

Edit `titles.txt` to fit your niche.

### Hashtags — randomly sampled from `hashtags.txt`

`hashtags.txt` in the repo holds your full pool (one tag per line, `#`
optional). The tool picks **8–12 random tags per post** (configurable),
so each reel's caption looks fresh and humanized.

Edit `hashtags.txt` to fit your niche (the default is bird/nature based
on the source folder you shared). Comments start with `# ` and blank
lines are ignored.

### Final caption shape

```
A bunch of pink baby birds hugging

#adorableanimals #foryou #amazing #viralreels #wow #viralvideos #birdwatching #videooftheday #wildlife #instagood #love #shortsfeed
```

You can tune via repo **variables** (Settings → Variables → Actions):

| Variable                 | Default | Notes                                |
| ------------------------ | ------- | ------------------------------------ |
| `HASHTAGS_PER_POST_MIN`  | `8`     | Min tags sampled per post (inclusive) |
| `HASHTAGS_PER_POST_MAX`  | `12`    | Max tags sampled per post (inclusive) |
| `HASHTAGS_FILE`          | `hashtags.txt` | Path inside the repo          |

---

## 4. State files (committed back to repo)

- `state/posted.json` — every Drive file ID already posted (dedup source of truth).
- `state/schedule_YYYY-MM-DD.json` — that day's chosen slots and which were posted.

The workflow has `permissions: contents: write` and pushes these after
every run. Two posts can't collide because of the `concurrency` group.

---

## 5. Cost

- **GitHub Actions free tier**: 2,000 minutes/month on private repos
  (unlimited on public). Each run is short (< 1 min when no slot is due,
  ~2–4 min when posting). At 48 runs/day worst-case that's well within
  free tier.
- **Google Drive API**: 1 billion queries/day free, no billing card.
- **Meta Graph API**: free.

---

## 6. Manual test locally (optional)

```bash
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
# install ffmpeg via your OS package manager

export FB_PAGE_ID=...
export FB_PAGE_TOKEN=...
export DRIVE_FOLDER_ID="https://drive.google.com/drive/folders/XXXX"
export DRY_RUN=true
python -m src.main
```

---

## 7. Troubleshooting

- **"No slot due; exiting"** — normal. The next cron tick will check again.
- **"All videos already posted"** — add more files to your Drive folder.
- **Facebook upload `(#100) Invalid parameter`** — your token is missing
  `publish_video` or `pages_manage_posts`, or the file is not a valid
  MP4 (H.264 + AAC, < 1GB, < 90s for Reels). The enhance step normalizes
  most of this automatically.
- **Page token expired** — rerun `scripts/get_long_lived_token.py`. If
  you call the API at least once every 60 days the token doesn't expire,
  but rotating it is harmless.
