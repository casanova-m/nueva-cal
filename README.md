# Family Calendar Filter

Merges multiple school iCalendar feeds into **one** filtered subscription you add to Google Calendar. Source changes propagate automatically. No copy-paste. No checkbox-juggling.

## What it does

Every 2 hours, GitHub's servers:

1. Fetch all your source webcal feeds
2. Filter events using rules in `config.yaml`
3. Deduplicate events that appear in multiple feeds (e.g., a 5th-grade event that's in both Middle School and Whole School feeds)
4. Publish one combined `.ics` file at a stable URL
5. Google Calendar polls that URL on its own schedule (usually within a few hours)

You subscribe **once** and never touch it again — unless you want to tune the filter rules, which is just editing one file.

---

## One-time setup

You'll do this on a computer (not phone) the first time. After that, no maintenance.

### 1. Make a GitHub account

Go to <https://github.com/signup>. Free. Username can be anything.

### 2. Create your repo from this template

- If you've been given this as a zip file: unzip it, then on GitHub click **+** (top right) → **New repository**. Drag the files in once it's created.
- Name the repo whatever you want, e.g. `family-cal`.
- Set it to **Public**. (Private GitHub Pages needs a paid plan. Your feed URLs are protected by GitHub Secrets — they never appear in the repo. Your output URL is protected by an unguessable token you choose.)
- Click **Create repository**.

### 3. Add your feed URLs as Secrets

In your new repo:

- **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
- Add each of these one at a time. The **Name** must match exactly. The **Secret** value is the full URL (webcal:// or https:// — both work).

| Secret name | Value |
|---|---|
| `FEED_WHOLE_SCHOOL` | Nueva whole-school webcal URL |
| `FEED_MIDDLE_SCHOOL` | Middle School webcal URL |
| `FEED_UPPER_SCHOOL` | Upper School webcal URL |
| `FEED_MAIA_GAMES` | Maia's games & practices URL |
| `FEED_ZANDER_GAMES` | Zander's games & practices URL |
| `OUTPUT_TOKEN` | Any random string you make up, e.g. `k7m2x9p4q` — becomes part of the output filename so the URL isn't guessable |

### 4. Enable GitHub Pages

- **Settings** → **Pages**
- **Source:** *GitHub Actions* (in the dropdown). That's all — no branch to pick.

### 5. Run the workflow for the first time

- **Actions** tab → **Build family calendar** (left sidebar) → **Run workflow** → green **Run workflow** button.
- Wait ~1 minute. Refresh. You should see a green checkmark.

### 6. Find your URLs

- **Settings** → **Pages**. You'll see your site URL at the top, like:
  `https://yourusername.github.io/family-cal/`
- Visit that URL in a browser. You'll see the **preview page** — a list of every event that was included, every event that was excluded, and why.
- Your **calendar subscription URL** is the site URL + `family-<your OUTPUT_TOKEN>.ics`, e.g.:
  `https://yourusername.github.io/family-cal/family-k7m2x9p4q.ics`
- (Easier: right-click the "Subscription file" link on the preview page → Copy link address.)

### 7. Subscribe in Google Calendar

- Open <https://calendar.google.com> **on a computer** (mobile apps don't expose this option, but once you subscribe on web, the calendar shows up automatically in the iOS/Android Google Calendar apps).
- Left sidebar: **Other calendars** → **+** → **From URL**.
- Paste your `family-<token>.ics` URL.
- **Add calendar**.

Done. Events trickle in within a few minutes. From now on, the workflow runs every 2 hours and Google checks for updates a few times a day.

---

## Tuning the filter

The preview page (`https://yourusername.github.io/family-cal/`) is your dashboard. Look at it occasionally — especially the first week.

**Something is in your calendar that shouldn't be?**

1. Find the event in the **Included** list on the preview page. Note a distinctive word from its title.
2. In GitHub, open `config.yaml`. Click the pencil icon (top right of the file view).
3. Add that word as a new line under `exclude_keywords:`. Keep the same indentation as the others.
4. Scroll down, click **Commit changes**.
5. The workflow re-runs automatically. Within ~2 minutes, refresh the preview page and check.

**Something is missing?**

1. Find it in the **Excluded** list. Note a word that would only match the events you DO want.
2. Add it under `include_keywords:` in `config.yaml`. Commit.

**Include beats exclude.** If an event title says "11th & 12th Grade Mixer", it stays in because "11th grade" matches an include keyword — even though "12th grade" matches an exclude.

---

## Common situations

**The school issued new feed URLs (token rotation, etc.)**
Update the matching secret(s) from step 3. Nothing else changes.

**You want to add a new feed later** (e.g., SCU calendar for the college kid)

1. Add a new secret in step 3 (e.g., `FEED_SCU`).
2. Edit `config.yaml`: add a new entry under `feeds:`:
   ```yaml
   - name: scu
     url_env: FEED_SCU
   ```
   And add `scu: include` (or `exclude`) under `feed_defaults:`.
3. Edit `.github/workflows/build.yml`: add a line under `env:`:
   ```yaml
   FEED_SCU: ${{ secrets.FEED_SCU }}
   ```
4. Commit.

**A workflow run failed (red X in Actions tab)**
Click into the failed run to see the error. 95% of the time it's a typo in a secret name. Compare against the table in step 3 exactly.

**You want changes to show up faster than every 2 hours**
Edit `.github/workflows/build.yml`: change `'15 */2 * * *'` to `'*/30 * * * *'` for every 30 minutes. Don't go below that — GitHub may throttle.

---

## What's actually in the repo

- `filter_calendar.py` — the script. Fetches, filters, dedupes, writes output.
- `config.yaml` — your filter rules. **This is the only file you normally edit.**
- `requirements.txt` — Python library versions.
- `.github/workflows/build.yml` — schedules the script and publishes to GitHub Pages.
- `README.md` — this file.

---

## Privacy notes

- Source feed URLs live only in GitHub Secrets. Even if the repo is public, the secrets are not visible to anyone (not even logged-in collaborators by default).
- The output `.ics` file is on a public URL but uses your `OUTPUT_TOKEN` so it isn't guessable — same security model as the webcal URLs the school gives you.
- If you ever feel the URL has leaked, just change `OUTPUT_TOKEN` to a new random string. The old URL becomes a 404 and a new one appears. (You'd need to unsubscribe and re-subscribe in Google Calendar.)
