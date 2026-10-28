# Show data template

Copy this folder somewhere outside the engine checkout, ideally into its own private git repo, and point `DATA_DIR` at it in `.env`:

```bash
cp -r examples/show-data ~/my-show-data && cd ~/my-show-data && git init
echo "DATA_DIR=$HOME/my-show-data" >> /path/to/podcast_server/.env
```

| File | Purpose |
| :--- | :--- |
| `PLAYBOOK.md` | Your editorial voice, title rules, review checklist, and workflows. Agents read it first. |
| `templates/social_*.txt` | Social post templates (`{hook}`, `{specs}`, `{guest_name}`, `{link}`, `{title}`) |
| `excluded_episodes.json` | Slugs/GUIDs that must never be (re)published. Delete the example entry. |
| `transcripts/` | `<slug>.txt` transcripts |
| `catalog.db` | Created on first run. Don't commit it; back it up with `scripts/backup_to_hf.py`. |
