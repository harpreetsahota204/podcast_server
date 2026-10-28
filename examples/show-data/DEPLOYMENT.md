# <Your Show> — Deployment Record

<!--
Template. Copy into your private DATA_DIR and replace every <placeholder>.
This is the record of what runs where, so that you (or an agent) can change DNS,
rotate credentials, or rebuild the setup on a new machine without guessing.
Record WHERE secrets live, never the secret values themselves.
Update it whenever infrastructure changes, and note the date at the top.
-->

Last verified: <YYYY-MM-DD>

## Show

| | |
| :--- | :--- |
| Show | <name> |
| Public feed | <PODCAST_FEED_URL> |
| Cover art | <PODCAST_IMAGE_URL> (source file: <path to the original artwork>) |
| Directories | <Apple Podcasts / Spotify / ... and the URL each one has on file> |
| Episodes | <count>; excluded: <count, see excluded_episodes.json> |

## Repositories and Paths

| What | Where |
| :--- | :--- |
| Engine checkout | <path to podcast_server> |
| Show data (`DATA_DIR`, this folder) | <path> (<private git remote, if any>) |
| Website repo (astro mode) | <path> (<remote>, <host, e.g. Netlify/Vercel/Cloudflare Pages>, deploys from <branch>) |

## Engine Configuration (non-secret values from `.env`)

| Setting | Value |
| :--- | :--- |
| `SITE_MODE` | <none / astro> |
| `R2_BUCKET_NAME` / `R2_PUBLIC_DOMAIN` | <bucket> / <https://audio.example.com> |
| `USE_OP3_PREFIX` | <true/false> |
| `EPISODE_GUID_PREFIX` | <prefix> |
| `WEBSITE_*` overrides | <any non-default site paths or branch> |
| `HF_BACKUP_REPO` | <user/dataset, or none> |
| Metrics sources | <op3 / cloudflare / x / extra collectors>; `CLOUDFLARE_ZONE_ID` <zone>, `OP3_SHOW_UUID` <uuid> |

## Metrics

- Store: `metrics.db` in this folder (disposable; re-collect within each source's retention)
- Dashboard: `metrics/dashboard.html`
- Schedule: <cron line or systemd timer name and where its unit files live>
- Retention to watch: <e.g. Cloudflare free plan keeps ~30 days, so a missed week of collection loses data>
- Public stats page: <OP3 stats URL, if the prefix is on>

## YouTube Thumbnails

- Style file: `templates/thumbnail_style.json`; fonts: <files in templates/fonts/ and their licenses>
- Art prompt style (`THUMBNAIL_STYLE`): <value>
- Change log: `social/youtube_thumbnails.jsonl` (pinned on the metrics dashboard)

## Audio Hosting (Cloudflare R2)

- Bucket: <name>; episodes at `episodes/<slug>.mp3`; media at `media/`
- Custom domain: <audio.example.com> (connected <date>)
- `r2.dev` public URL: <disabled / still enabled because ...>
- Reminder: published objects are cached as immutable; never overwrite a key.

## DNS

Zone on Cloudflare since <date>. Registrar: <registrar>. Nameservers: <ns1>, <ns2>. DNSSEC: <on/off>.

| Record | Value | Notes |
| :--- | :--- | :--- |
| `A @` / `CNAME @` | <site host target> | <e.g. Netlify: DNS-only, never proxied> |
| `CNAME www` | <target> | |
| `MX @` | <mail servers + priorities> | **Email. Never remove.** |
| `TXT @` | <SPF, site verifications> | |
| `TXT <selector>._domainkey` | <DKIM> | Email signing |
| `<audio subdomain>` | managed by the R2 custom domain | |

## Website Wiring (astro mode)

- Episode pages: <WEBSITE_EPISODES_DIR>
- Feed file: <WEBSITE_RSS_FILE>
- Blog posts: <WEBSITE_BLOG_DIR>
- Transcripts / other generated assets: <paths, if any>
- Security headers / CSP must allow: <audio domain, op3.dev if enabled>
- Untracked work that must never be published: <drafts, scripts>

## Backups

| What | Where | How often |
| :--- | :--- | :--- |
| Audio + catalog snapshot + show assets | <HF dataset or other> | <after every publish> |
| Show data repo | <remote> | <on commit> |
| Other | <e.g. archive of episodes from a previous host> | |

How to restore: <short steps>

## Agents and Credentials (locations only)

| Credential | Lives in | Scope / notes |
| :--- | :--- | :--- |
| R2 API token | `.env` | <bucket-scoped, Object Read & Write> |
| Git push to website | <credential helper / token> | <scopes> |
| Cloudflare (DNS changes) | <dashboard / CLI login> | |
| YouTube OAuth | <YOUTUBE_TOKEN_PATH> | |
| Descript | `.env` | |
| OP3 API token | `.env` | <read-only API key> |
| Cloudflare analytics token | `.env` | <Analytics Read in a policy scoped to one zone/domain> |
| X (posting + metrics) | <xurl auth file> | <app name; scopes> |
| Agent(s) operating the show | <agent name, where its MCP config and briefing live> | |

## Timeline

1. <date>: <what changed>

## Pending

- [ ] <e.g. cancel the previous host after the redirect window ends on <date>>

## Troubleshooting Specific to This Setup

| Symptom | Cause and fix |
| :--- | :--- |
| <symptom> | <cause and fix> |
