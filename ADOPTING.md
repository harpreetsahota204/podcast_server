# Run Your Own Podcast on `podcast_server`

This guide takes you from a fresh clone to a live feed that Apple Podcasts and Spotify can subscribe to, either as a new show or by moving an existing one off a hosting service without losing subscribers.

**What you end up with:** your audio in your own Cloudflare R2 bucket on your own domain, your catalog in a SQLite file you can open and inspect, and an RSS feed you control. An AI agent (e.g. Claude Desktop or Cursor) does the publishing through MCP tools; you approve each episode before it goes live.

**What it costs:** R2 storage is about $0.015 per GB per month, and R2 doesn't charge for downloads. A 150-episode archive (~10 GB) is roughly $0.15/month. A domain is the only other required cost.

**Time:** about 45 minutes for a new show, plus a waiting period of a few weeks if you're migrating.

---

## Before You Start

You need:

- **A domain whose DNS is on Cloudflare** (the free plan works). R2 custom domains only work on Cloudflare-managed zones.
- **[uv](https://docs.astral.sh/uv/)** (`curl -LsSf https://astral.sh/uv/install.sh | sh`) and git. uv installs the right Python (3.11+) for you.
- **Cover art**: a square JPEG or PNG, 1400–3000 px, hosted at a public URL. Apple rejects feeds whose artwork doesn't load.
- **Optional:** a website that deploys from git (Netlify, Vercel, Cloudflare Pages) if you want an episode page per episode.

Then make two choices:

| Choice | Options |
| :--- | :--- |
| **Website mode** (`SITE_MODE`) | `none`: no website needed; the feed is served from your R2 domain (e.g. `https://audio.example.com/feed.xml`). Simplest. <br> `astro`: the engine writes the feed plus one Markdown page per episode into your site repo and pushes it, so your host rebuilds the site. |
| **Starting point** | **New show**: follow steps 1–4, skip step 5. <br> **Migrating from another host**: steps 1–3, then step 5 before anything else goes public. |

---

## Step 1: Cloudflare (about 15 minutes)

1. **Put your domain on Cloudflare** if it isn't already: Cloudflare dashboard → *Add a domain* → Free plan.
   - Before you switch nameservers at your registrar, confirm Cloudflare copied **every** existing DNS record, especially **email (MX, SPF/TXT, DKIM)**. A missing MX record silently breaks your email.
   - If your site is on Netlify, set its records to **DNS only** (grey cloud). Proxying them through Cloudflare breaks Netlify's TLS certificates.
   - If DNSSEC is enabled at your registrar, turn it off before switching nameservers, or the domain stops resolving.
2. **Create an R2 bucket** (R2 → *Create bucket*), e.g. `my-show-audio`.
3. **Connect a custom domain** to the bucket: bucket → *Settings* → *Custom Domains* → `audio.example.com`. Don't use the bucket's `r2.dev` URL in production: it's rate-limited and uncached.
4. **Create an R2 API token** (R2 → *Manage API tokens*) with *Object Read & Write* on that bucket only. Note the Account ID, Access Key ID, and Secret.

## Step 2: Install and Configure (about 10 minutes)

```bash
git clone <this repo> podcast_server && cd podcast_server
uv sync                               # creates .venv with locked dependencies
cp .env.example .env && chmod 600 .env
```

Fill in `.env`; every variable is documented inline. The required ones:

| Variable | Example | Notes |
| :--- | :--- | :--- |
| `PODCAST_TITLE`, `PODCAST_DESCRIPTION`, `PODCAST_AUTHOR` | | Shown in every podcast app |
| `PODCAST_OWNER_EMAIL` | `you@example.com` | Apple contacts this address; also locks the feed to you |
| `PODCAST_LINK` | `https://example.com/podcast` | Show homepage |
| `PODCAST_FEED_URL` | `https://audio.example.com/feed.xml` | `none` mode: `R2_PUBLIC_DOMAIN/feed.xml`. `astro` mode: wherever your site serves `WEBSITE_RSS_FILE` |
| `PODCAST_IMAGE_URL` | `https://example.com/cover.jpg` | |
| `EPISODE_GUID_PREFIX` | `myshow` | New episodes get GUIDs like `myshow-<slug>-<timestamp>` |
| `R2_*` | | From step 1 |
| `R2_PUBLIC_DOMAIN` | `https://audio.example.com` | The custom domain, not `r2.dev` |

Keep your show's data, and your editorial process, outside the code checkout, so you can pull engine updates cleanly and nothing private ever lands in this repo. Start from the template:

```bash
cp -r examples/show-data ~/my-show-data && (cd ~/my-show-data && git init)   # make it a private repo
echo "DATA_DIR=$HOME/my-show-data" >> .env
```

`DATA_DIR` holds the catalog (`catalog.db`, created on first run), your **`PLAYBOOK.md`** (voice, title rules, review checklist, workflows), your **`DEPLOYMENT.md`** (a record of your DNS, R2, website, backups, and where credentials live; fill it in as you go through these steps), social templates, the exclusion list, transcripts, and backup staging. The template's `.gitignore` keeps the catalog and audio out of git.

**Verify everything:**

```bash
uv run python -m core.doctor
```

It checks your R2 credentials, that the audio domain resolves, the website or feed hosting setup, the cover art, and the feed URL, and prints the fix for anything that fails. (`Feed URL` warns until your first publish; that's expected.)

## Step 3: Connect Your Agent

The MCP server runs over stdio. Point your agent at `bin/run-mcp.sh` in your checkout, e.g. in Cursor or Claude Desktop:

```json
{
  "mcpServers": {
    "podcast_server": { "command": "/absolute/path/to/podcast_server/bin/run-mcp.sh" }
  }
}
```

Ask the agent to run `podcast_status`; it should report your show title and `r2_storage_ready: true`. Point it at [`AGENTS.md`](./AGENTS.md) (generic runbook and safety rules) and at your `$DATA_DIR/PLAYBOOK.md`. Fill in the playbook before your first episode: it's how the agent learns your voice, title formula, and review checklist.

## Step 4: Publish and Submit (new shows)

Ask the agent: *"Episode 1 is ready at `~/recordings/ep1.mp3`. Prep it."* It inspects the audio, drafts titles, show notes, and chapters, and stops for your approval. On approval it calls `podcast_publish`, which:

1. Rejects the episode if its slug is invalid, already used, or excluded, **before** uploading anything.
2. Measures the exact duration and byte length, then uploads the MP3 to R2.
3. Adds the episode to `catalog.db` with a new, permanent GUID.
4. Regenerates the feed: uploaded to R2 in `none` mode, or written into your site and pushed in `astro` mode.

No agent? Call the tool function directly:

```bash
uv run python -c "
from mcp_server.server import podcast_publish
print(podcast_publish(audio_file_path='ep1.mp3', title='Episode 1: ...', description='One-line summary',
                      show_notes_markdown='### Chapters\n\n- **00:00** — Intro', slug='episode-1'))"
```

Once the feed is live, submit `PODCAST_FEED_URL` once in **Apple Podcasts Connect** and **Spotify for Creators**. New episodes appear on their own after that.

## Step 5: Migrating From Another Host

Every podcast host publishes RSS, so the importer works with any of them. The rule that matters most: **every episode keeps its original GUID**. Apps track played/unplayed state by GUID, so a new GUID makes every subscriber's app show the whole archive as new.

1. **Import and mirror the audio**:

   ```bash
   uv run python -m importers.rss https://old-host.example/feed.rss --mirror
   ```

   This copies every MP3 into your bucket and records the byte length R2 actually stores, since old hosts often report lengths a few hundred bytes off. Re-running it is safe: existing episodes are never overwritten. Episodes whose audio hasn't been mirrored are kept out of the public feed automatically.
2. **Check the result.** Run `podcast_status` (episode count should match your old host), then `python -m core.site` to build the feed, and compare a few episodes' GUIDs against the old feed.
3. **Leaving episodes behind** (e.g. bonus content): add them to `DATA_DIR/excluded_episodes.json` and they can never be imported or published:

   ```json
   {"episodes": [{"slug": "old-bonus-1", "guid": "<original guid>", "title": "Old Bonus 1"}]}
   ```

4. **Remove dependencies on the old host.** Search your show notes and website for the old host's domain: embedded players, transcript links, artwork URLs. Everything that points there breaks when you cancel.
5. **Redirect the old feed** in the old host's dashboard: a 301 redirect to your new `PODCAST_FEED_URL` (some hosts also add `<itunes:new-feed-url>`). Apple and Spotify follow it; you don't need to resubmit.
6. **Wait 2–4 weeks** before cancelling, so every app has picked up the redirect. Back up first (below).

## Optional Integrations

**YouTube uploads** (`podcast_publish_to_youtube`):
1. In Google Cloud Console, create a project, enable **YouTube Data API v3**, and create an **OAuth client ID** of type *Desktop app*. Download it as `client_secret.json`.
2. Authorize once (opens a browser) to produce the token file the engine reads:

   ```bash
   uv run --with google-auth-oauthlib python -c "
   from google_auth_oauthlib.flow import InstalledAppFlow
   flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', ['https://www.googleapis.com/auth/youtube.upload'])
   open('youtube_token.json', 'w').write(flow.run_local_server(port=0).to_json())"
   ```

3. Move `youtube_token.json` somewhere private, `chmod 600` it, and set `YOUTUBE_TOKEN_PATH` in `.env`. Uploads default to `unlisted`.

**Descript staging** (`podcast_stage_from_descript`): the tool takes the episode's transcript exported from Descript as Markdown (markers such as `[00:12:30] Chapter title` or timestamped headings become chapters), plus either the published audio's download URL or a local MP3 path. Set `DESCRIPT_API_BEARER` only if your download URLs require authentication; it's sent to `descript.com` hosts only.

**Backups:** `hf auth login` once, set `HF_BACKUP_REPO` to a dataset id under your account (it's created private on first run), then run the backup after each publish (below).

## Day-to-Day

| Task | How |
| :--- | :--- |
| Publish | Agent: `podcast_stage_from_descript` or `podcast_inspect` → your approval → `podcast_publish` |
| Rebuild the feed (and pages) | `podcast_rebuild_feed` tool, or `uv run python -m core.site` |
| Back up | Set `HF_BACKUP_REPO`, run `hf auth login` once, then `uv run python scripts/backup_to_hf.py` after each publish. It mirrors every MP3, a catalog snapshot, and your show assets to a **private** Hugging Face dataset, uploading only what changed. |
| Re-check setup | `uv run python -m core.doctor` |

## Customizing

- **Editorial voice and workflows:** `DATA_DIR/PLAYBOOK.md` (template in `examples/show-data/`). Agents follow it for titles, show notes, and review.
- **Social posts:** put `social_long.txt` / `social_short.txt` in `DATA_DIR/templates/` using `{hook}`, `{specs}`, `{guest_name}`, `{link}`, `{title}` (double literal braces: `{{ }}`).
- **Site layout (astro mode):** `WEBSITE_RSS_FILE`, `WEBSITE_EPISODES_DIR`, `WEBSITE_BLOG_DIR`, `WEBSITE_BRANCH`. Episode pages use this frontmatter; add a matching collection schema to your site:

  ```ts
  // src/content.config.ts (Astro)
  const podcast = defineCollection({
    schema: z.object({
      title: z.string(), description: z.string(), pubDate: z.coerce.date(),
      audioUrl: z.string().url(), duration: z.string(), durationSeconds: z.number(),
      fileSizeBytes: z.number(), episodeNumber: z.number().nullable(), season: z.number(),
      explicit: z.boolean(), guid: z.string(), coverArt: z.string(), tags: z.array(z.string()),
      draft: z.boolean(),
    }),
  });
  ```

  Any static site generator that reads YAML frontmatter from Markdown can use these files.
- **Several shows:** one `.env` and one `DATA_DIR` per show; select with `PODCAST_SERVER_ENV_FILE=/path/to/show.env`.

## Things We Learned the Hard Way

| Rule | Why |
| :--- | :--- |
| Never change or regenerate a GUID | Duplicate episodes on every subscriber's device |
| Never overwrite a published audio file | Objects are cached as immutable for a year; listeners keep getting the old file. Upload under a new name. |
| Use a custom domain, not `r2.dev` | `r2.dev` is rate-limited and uncached |
| Byte lengths come from R2, not the old host | Wrong `<enclosure length>` values; backups re-download everything |
| Copy MX/SPF/DKIM before moving DNS | Email stops working silently |
| Netlify records stay DNS-only on Cloudflare | Proxying breaks Netlify's certificates |
| Headless Linux: use `credential.helper store` (or a fine-grained token) for the site repo | Keyring helpers like libsecret can't unlock without a desktop session, so deploy pushes fail |
| Deploys commit only generated files | Your unpublished work in the site repo never goes live by accident |
| OP3 analytics are public | Anyone can look up your download numbers; leave `USE_OP3_PREFIX=false` if that matters |

## Current Limits

- MP3 only (`audio/mpeg`); the importer skips other enclosure types and lists them.
- Storage is Cloudflare R2 (S3 API; other S3-compatible stores need small changes in `core/storage.py`).
- `astro` mode writes Markdown + YAML frontmatter and deploys by git push; other site setups use `SITE_MODE=none`.
- Transcription and semantic search are planned, not built (see [`PLAN.md`](./PLAN.md)).

## License

[Business Source License 1.1](./LICENSE): free to run for your own show, open-source, or internal business use; you may not offer it as a commercial hosted podcast service. It converts to Apache 2.0 on October 26, 2029.
