# Show data template

Copy this folder somewhere outside the engine checkout, ideally into its own private git repo, and point `DATA_DIR` at it in `.env`:

```bash
cp -r examples/show-data ~/my-show-data && cd ~/my-show-data && git init
echo "DATA_DIR=$HOME/my-show-data" >> /path/to/podcast_server/.env
```

| File | Purpose |
| :--- | :--- |
| `PLAYBOOK.md` | Your editorial voice, title rules, review checklist, and workflows. Agents read it first. |
| `DEPLOYMENT.md` | Your infrastructure record: DNS (including email records), R2, website wiring, backups, where credentials live, pending items. Fill it in as you set things up. |
| `templates/social_*.txt` | Social post templates (`{hook}`, `{specs}`, `{guest_name}`, `{link}`, `{title}`) |
| `templates/thumbnail_style.json` + `templates/fonts/` | YouTube thumbnail look: fonts, colors, fade, text box (template in this folder) |
| `excluded_episodes.json` | Slugs/GUIDs that must never be (re)published. Delete the example entry. |
| `transcripts/` | `<slug>.txt` transcripts |
| `catalog.db` | Created on first run. Don't commit it; back it up with `scripts/backup_to_hf.py`. |
| `social/` | Written by the engine: append-only logs of every public action and draft (`x_posts.jsonl`, `instagram_posts.jsonl`, `youtube_uploads.jsonl`, `youtube_thumbnails.jsonl`, `devto_posts.jsonl`, `substack_posts.jsonl`). Agents read them before retrying a failed call; the metrics collectors read them to know which posts are the show's. Keep them (commit or back up). |
| `youtube_videos.json` | Optional: videos uploaded outside the engine, mapped to episodes, so YouTube metrics cover them |
| `thumbnails/` | Staged thumbnail art and rendered thumbnails |
| `metrics.db`, `metrics/dashboard.html` | Collected audience metrics and the offline dashboard. Disposable: re-collect any time. |
| `backup/` | Backup staging and the last backup log |
