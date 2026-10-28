# <Your Show> — Show Playbook

<!--
Template. Copy this folder to your private DATA_DIR and replace every <placeholder>.
Agents read this file before any podcast task. Keep it private: it is your
editorial voice and process, and it never belongs in the engine repository.
-->

## 1. Show Identity

- **Show:** <name>, hosted by <host(s)>
- **Audience:** <who listens and what they want from an episode>
- **Formats:** <e.g. long-form interviews, solo deep dives, panels> and typical length

## 2. Voice & Style

- **Tone:** <e.g. direct and technical / warm and conversational>
- **Do:** <habits to follow, e.g. name the mechanism, state trade-offs, use concrete numbers>
- **Don't:** <words and patterns to avoid>
- **Formatting rules:** <capitalization, punctuation, emoji/hashtag policy>

## 3. Titles

- **Formula:** <e.g. "<Topic>: <Specific angle> with <Guest>">
- **Good examples:** <2–3 titles you like>
- **Avoid:** <e.g. episode-number-only titles, clickbait>
- **Options per episode:** <how many title options to draft for review>

## 4. Show Notes

- **Summary:** <length and what it must cover>
- **Chapters:** bold timestamps, e.g. `- **06:40** — <chapter title>`
- **Sections to include:** <guest bio, links, resources, sponsor read, ...>
- **Companion content:** <blog post? newsletter? where it goes and its standards>

## 5. Review Checklist (the human gate)

Before asking for approval, the agent presents:
- [ ] <N> title options
- [ ] Summary and chapters
- [ ] <Companion post outline, social drafts, thumbnail prompt, ...>

Never publish without explicit approval.

## 6. Workflows

### <Workflow name, e.g. "Recording ready in Descript">
1. <step>
2. <step>
3. Human review gate
4. `podcast_publish(...)`, then verify and back up

### <Another workflow>
1. <step>

## 7. Distribution

- **Social:** <platforms; templates live in templates/social_long.txt and social_short.txt>
- **Video:** <YouTube privacy default, description format, chapters>
- **Thumbnails:** <visual style; also set THUMBNAIL_STYLE in .env>

## 8. Deployment

Infrastructure details (DNS, R2, website wiring, backups, credentials) live in `DEPLOYMENT.md` next to this file. Keep this playbook about voice and process.

## 9. Show-Specific Rules

- <e.g. episodes that must never be republished: listed in excluded_episodes.json>
- <anything else agents must never do for this show>
