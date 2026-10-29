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
5. <Optional: once the companion blog post is live, `podcast_crosspost_to_devto` / `podcast_crosspost_to_substack`, then hand the draft URLs to the human>
6. <Seven days later: `podcast_episode_metrics(slug)` and report first-week numbers per source>

### <Another workflow>
1. <step>

## 7. Distribution

- **Social:** <platforms; templates live in templates/social_long.txt and social_short.txt; always pass episode_slug to podcast_post_to_x and podcast_post_to_instagram>
- **Blog cross-posting:** <which platforms companion posts go to (dev.to, Substack), the 4 dev.to tags to use (lowercase letters and digits only), whether Substack drafts are emailed to subscribers>. Flow: blog post live on the site → podcast_crosspost_to_devto / podcast_crosspost_to_substack → human publishes each draft on its platform.
- **Video:** <YouTube privacy default, description format, chapters; always pass episode_slug to podcast_publish_to_youtube>
- **Thumbnails:** <art style (also THUMBNAIL_STYLE in .env), headline rules (word count, tone), label format>. Fonts, colors, and layout go in templates/thumbnail_style.json. Flow: podcast_stage_thumbnail → agent renders art → podcast_render_thumbnail → approval → podcast_set_youtube_thumbnail.
- **Metrics:** <which numbers matter for this show, review cadence (e.g. 7 days after each release)>; agents read them with podcast_metrics_summary / podcast_episode_metrics.

## 8. Deployment

Infrastructure details (DNS, R2, website wiring, backups, credentials) live in `DEPLOYMENT.md` next to this file. Keep this playbook about voice and process.

## 9. Show-Specific Rules

- <e.g. episodes that must never be republished: listed in excluded_episodes.json>
- <anything else agents must never do for this show>
