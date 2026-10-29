"""Posts to the show's Instagram professional account through Meta's Graph API (graph.facebook.com).

Publishing is Meta's two-step flow: create a media container from a public URL, wait
until Meta has fetched and processed it, then publish the container. Meta downloads the
media itself, so the engine first converts each image to the JPEG Instagram requires and
stages it on the public R2 domain under a key derived from the source file's hash: a
retry reuses the same object, and nothing already public is ever overwritten.

Every accepted post is logged to `$DATA_DIR/social/instagram_posts.jsonl`; the same image
with the same caption is never posted twice from that log.

`login` accepts three kinds of token and tells them apart by what Meta returns:
- Facebook login, user token (e.g. long-lived, from Graph API Explorer): `/me/accounts`
  lists the Pages the user manages with each Page's own token; the engine saves the token
  of the Page linked to the Instagram account, which never expires, and never the user token.
- Facebook login, Page token: `/me` is the Page, and the account is its
  `instagram_business_account`.
- Unified login (Meta's setup for new apps): a Business Integration System User token.
  `/me` is the system user, and the accounts shared with it are listed under
  `/me/assigned_instagram_accounts`; its expiry is set in the app's login configuration.
The saved token controls the account, so it lives in its own 0600 file (INSTAGRAM_TOKEN_PATH),
never in .env, URLs, logs, or tool output, and is sent to Meta only in an Authorization header:
    uv run python -m exporters.instagram login     # save a token
"""

import hashlib
import json
import re
import sys
import tempfile
import time
import urllib.error
from datetime import UTC, datetime
from getpass import getpass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlencode

from PIL import Image

from core.config import settings
from core.credentials import write_private_json
from core.formatting import validate_slug
from core.jsonl import append_record, read_records
from core.net import open_https
from core.storage import R2StorageClient

# Both supported logins call the Facebook Graph host; the version is pinned so a new one can't change reply shapes.
GRAPH_URL = "https://graph.facebook.com/v26.0"
# Instagram's caption limits.
MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30
_HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
# Feed images: JPEG only, aspect ratio between 4:5 and 1.91:1, at most 8 MB; wider than 1440 px is scaled down by Meta.
IMAGE_SOURCE_TYPES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MIN_ASPECT, MAX_ASPECT = 4 / 5, 1.91
MAX_IMAGE_WIDTH = 1440
MAX_IMAGE_BYTES = 8 * 1024 * 1024
JPEG_QUALITY = 90
# Staged media lives under its own prefix in the public bucket, next to episodes/ and media/.
R2_PREFIX = "instagram"
# Images usually finish in seconds; the timeout bounds an MCP call if Meta stalls.
CONTAINER_POLL_SECONDS = 3
CONTAINER_TIMEOUT_SECONDS = 300
# Meta error code for an expired, revoked, or otherwise invalid access token.
INVALID_TOKEN_CODE = 190
# Meta's delete endpoint answers this "Generic Internal Error" intermittently; nothing was deleted when it does.
TRANSIENT_DELETE_SUBCODE = 2207085
# Listing: Meta pages at most 100 posts per request; captions are trimmed so a page stays readable.
MAX_LIST_LIMIT = 100
CAPTION_PREVIEW_CHARS = 120
POST_FIELDS = "id,media_type,media_product_type,timestamp,permalink,caption,username"
# Instagram media ids are numeric; anything else could address a different Graph object.
_MEDIA_ID_RE = re.compile(r"\d+")
# Meta's error text is returned to the agent; cap it so a long dump stays readable.
MAX_ERROR_CHARS = 300
LOGIN_COMMAND = "uv run python -m exporters.instagram login"
# Terminals can wrap a paste in bracketed-paste markers or carry line breaks from a wrapped copy;
# a token never contains whitespace or escape codes, so both are stripped before use.
_PASTE_NOISE_RE = re.compile(r"\x1b\[[0-9;]*~|\s")
# Page and system-user tokens start with this public type marker; Instagram Login tokens ("IG...") don't work here.
GRAPH_TOKEN_PREFIX = "EAA"  # noqa: S105 - a public token-type marker, not a secret
# The Instagram account linked to a Page, asked of a Page token's /me and of each Page in a user token's /me/accounts
# (braces percent-encoded for the URL).
PAGE_LINKED_ACCOUNT_FIELDS = quote("instagram_business_account{id,username}")

PostType = Literal["image"]


class InstagramError(RuntimeError):
    """Instagram posting isn't configured or its token is unusable, or Instagram rejected or didn't answer a request."""


def _describe_rejection(exc: urllib.error.HTTPError) -> str:
    """Meta's own explanation of a rejected request, plus the fix when the token is the problem."""
    body = exc.read().decode("utf-8", errors="replace")
    # Meta answers {"error": {"message", "code", ...}}, with the useful text in error_user_msg when "message" is
    # just "Fatal"; fall back to the raw body if it doesn't.
    try:
        error = json.loads(body)["error"]
        message = error.get("error_user_msg") or error["message"]
        code, subcode = error.get("code"), error.get("error_subcode")
    except (ValueError, KeyError, TypeError):
        message, code, subcode = body.strip(), None, None
    detail = f"Instagram HTTP {exc.code}: {message}"[:MAX_ERROR_CHARS]
    if code == INVALID_TOKEN_CODE:
        return f"{detail} The token is invalid, expired, or revoked: generate a new one and run `{LOGIN_COMMAND}`."
    if subcode == TRANSIENT_DELETE_SUBCODE:
        return f"{detail} Meta's delete endpoint fails this way at times; nothing was deleted, so retrying is safe."
    return detail


def _call(
    path: str, access_token: str, payload: dict[str, Any] | None = None, method: str | None = None
) -> dict[str, Any]:
    """GET (or POST `payload` as JSON to, or send `method` such as DELETE to) a Graph API path and decode the reply.

    The token travels only in the Authorization header, and errors never include the
    URL or headers, so it can't leak into a message.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    try:
        with open_https(f"{GRAPH_URL}/{path}", headers, data, method) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise InstagramError(_describe_rejection(exc)) from None
    except OSError as exc:
        raise InstagramError(f"Instagram didn't answer ({exc}).") from None


def _call_or_empty(path: str, access_token: str) -> dict[str, Any]:
    """Like `_call`, but any error yields an empty reply: for lookups whose failure is expected or harmless."""
    try:
        return _call(path, access_token)
    except InstagramError:
        return {}


def reachable_accounts(access_token: str) -> list[dict[str, str]]:
    """The Instagram accounts this token reaches, each with its id, username, and the token that posts to it.

    - Unified login token: the accounts shared with its system user, posted to with the same token.
    - Page token: the account linked to its Page, posted to with the same token.
    - User token (e.g. from Graph API Explorer): the accounts linked to the Pages the user manages, each
      posted to with that Page's own token, which never expires when the user token is long-lived.
    Raises InstagramError if Meta rejects the token itself.
    """
    # Checked first and allowed to raise: an invalid token must be reported as such, not as "no account".
    _call("me?fields=id", access_token)
    # Each lookup below only exists for one kind of token, so on the other kinds it fails and contributes nothing.
    shared = _call_or_empty("me/assigned_instagram_accounts?fields=id,username", access_token).get("data", [])
    linked = _call_or_empty(f"me?fields={PAGE_LINKED_ACCOUNT_FIELDS}", access_token).get("instagram_business_account")
    managed = _call_or_empty(f"me/accounts?fields=access_token,{PAGE_LINKED_ACCOUNT_FIELDS}", access_token)
    # Pair each account with the token that posts to it: the pasted one, or for a user token each Page's own.
    found = [(account, access_token) for account in [*shared, *([linked] if linked else [])]]
    found += [
        (page["instagram_business_account"], page.get("access_token"))
        for page in managed.get("data", [])
        if page.get("instagram_business_account")
    ]
    # Missing fields stay as empty strings so login can report them instead of treating the account as absent.
    return [
        {"user_id": str(a.get("id") or ""), "username": str(a.get("username") or ""), "access_token": str(t or "")}
        for a, t in found
    ]


def _save_token(account: dict[str, str]) -> None:
    """Write the account and the token that posts to it into INSTAGRAM_TOKEN_PATH (mode 600)."""
    saved = {**account, "saved_at": datetime.now(UTC).isoformat()}
    write_private_json(settings.instagram_token_path.expanduser(), saved)


def load_token() -> dict[str, str]:
    """The saved token and its account, read without contacting Instagram.

    Raises InstagramError if posting is off, the token is missing, or it was saved for
    an account other than INSTAGRAM_USERNAME. An expired token is reported by Meta on
    the first request, with the fix.
    """
    if not settings.instagram_username:
        raise InstagramError(f"Instagram posting is off: set INSTAGRAM_USERNAME, then run `{LOGIN_COMMAND}`.")
    path = settings.instagram_token_path.expanduser()
    if not path.is_file():
        raise InstagramError(f"No Instagram token at {path}; run `{LOGIN_COMMAND}`.")
    token = json.loads(path.read_text(encoding="utf-8"))
    # The token, not the setting, decides where a post lands, so a mismatch must stop it before any request.
    if str(token.get("username", "")).lower() != settings.instagram_username.lower():
        raise InstagramError(
            f"The saved token is for @{token.get('username')}, not INSTAGRAM_USERNAME @{settings.instagram_username}; "
            f"run `{LOGIN_COMMAND}` as the right account."
        )
    return token


def verify_account() -> dict[str, str]:
    """Check that the saved token still reaches its account on Instagram; return the account (for the doctor)."""
    token = load_token()
    if not any(a["user_id"] == token["user_id"] for a in reachable_accounts(token["access_token"])):
        raise InstagramError(f"The saved token no longer reaches @{token['username']}; run `{LOGIN_COMMAND}` again.")
    return {"username": token["username"], "saved_at": token["saved_at"]}


def validate_caption(caption: str) -> str:
    """Return the trimmed caption if Instagram accepts it (at most 2,200 characters and 30 hashtags), else raise."""
    text = caption.strip()
    if len(text) > MAX_CAPTION_CHARS:
        raise ValueError(f"Instagram captions allow {MAX_CAPTION_CHARS} characters; this one has {len(text)}.")
    if (hashtags := len(_HASHTAG_RE.findall(text))) > MAX_HASHTAGS:
        raise ValueError(f"Instagram allows {MAX_HASHTAGS} hashtags per caption; this one has {hashtags}.")
    return text


def _resolve_image(media_path: str) -> Path:
    """Return the image to post, refusing missing, empty, or non-image files (so an agent can't post e.g. `.env`)."""
    path = Path(media_path).expanduser().resolve()
    if path.suffix.lower() not in IMAGE_SOURCE_TYPES:
        raise ValueError(f"Unsupported image type {path.suffix!r}; allowed: {', '.join(sorted(IMAGE_SOURCE_TYPES))}")
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"Image file missing or empty: {path}")
    return path


def prepare_image(source: Path, output: Path) -> tuple[int, int]:
    """Write `source` as an Instagram-ready JPEG at `output` and return its (width, height).

    Raises ValueError for an aspect ratio Instagram rejects: cropping is an editorial
    choice, so the image is refused rather than cut.
    """
    with Image.open(source) as image:
        width, height = image.size
        ratio = width / height
        if not MIN_ASPECT <= ratio <= MAX_ASPECT:
            raise ValueError(
                f"{source.name} is {width}x{height} (aspect {ratio:.2f}); Instagram feed images must be "
                f"between 4:5 ({MIN_ASPECT:.2f}) and 1.91:1."
            )
        # JPEG has no alpha channel; RGB conversion also covers palette and grayscale sources.
        rgb = image.convert("RGB")
    if rgb.width > MAX_IMAGE_WIDTH:
        rgb = rgb.resize((MAX_IMAGE_WIDTH, round(rgb.height * MAX_IMAGE_WIDTH / rgb.width)), Image.Resampling.LANCZOS)
    rgb.save(output, "JPEG", quality=JPEG_QUALITY, optimize=True)
    if output.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"{source.name} is over Instagram's 8 MB limit even as a JPEG.")
    return rgb.size


def _stage_on_r2(jpeg: Path, source_sha256: str) -> str:
    """Upload the JPEG to the public bucket (unless a retry already did) and return the URL Meta will fetch."""
    client = R2StorageClient()
    if not client.is_configured():
        raise RuntimeError(client.config_error)
    # The key is the source file's hash, so an existing object is this same image, never a different one.
    key = f"{R2_PREFIX}/{source_sha256[:24]}.jpg"
    if not client.object_exists(key):
        client.upload_file(jpeg, key, content_type="image/jpeg")
    return client.public_url(key)


def _wait_until_finished(container_id: str, access_token: str) -> None:
    """Poll a media container until Meta has fetched and processed it; raise if it fails or takes too long."""
    deadline = time.monotonic() + CONTAINER_TIMEOUT_SECONDS
    while True:
        status = _call(f"{container_id}?fields=status_code,status", access_token)
        code = status.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            detail = status.get("status", "no detail")
            raise InstagramError(f"Instagram couldn't process the media ({code}: {detail}); nothing was published.")
        if time.monotonic() >= deadline:
            raise InstagramError(
                f"Instagram was still processing after {CONTAINER_TIMEOUT_SECONDS}s; nothing was published."
            )
        time.sleep(CONTAINER_POLL_SECONDS)


def _logged_post(source_sha256: str, caption: str) -> dict[str, Any] | None:
    """The engine's record of an earlier post of this same image and caption, unless the engine has since deleted it."""
    deleted = {record.get("media_id") for record in read_records(settings.instagram_deletion_log_path)}
    for record in reversed(read_post_log()):
        same = record.get("media_sha256") == source_sha256 and record.get("caption") == caption
        if same and record["media_id"] not in deleted:
            return {"media_id": record["media_id"], "url": record.get("url")}
    return None


def read_post_log() -> list[dict[str, Any]]:
    """Every Instagram post this engine published, oldest first; malformed lines (e.g. a torn write) are skipped."""
    return [r for r in read_records(settings.instagram_post_log_path) if r.get("media_id")]


def post_to_instagram(
    media_paths: list[str],
    caption: str = "",
    post_type: PostType = "image",
    dry_run: bool = False,
    episode_slug: str | None = None,
) -> dict[str, Any]:
    """Publish a feed post to INSTAGRAM_USERNAME and log it; return the media id and post URL.

    Every check runs before anything is uploaded. `dry_run` runs the same checks
    (including converting the image) and reports what would be posted without uploading
    or contacting Instagram. If the same image was already posted with the same caption,
    that post is returned instead. `episode_slug` credits the post to that episode.
    """
    # 1. Local checks: post type, media file, caption, episode, and the saved token (no network yet).
    if post_type != "image":
        raise ValueError(f"Unsupported post type {post_type!r}; only 'image' is available.")
    if len(media_paths) != 1:
        raise ValueError(f"An image post takes exactly one file; got {len(media_paths)}.")
    source = _resolve_image(media_paths[0])
    text = validate_caption(caption)
    slug = validate_slug(episode_slug) if episode_slug else None
    token = load_token()
    username = token["username"]
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

    with tempfile.TemporaryDirectory() as staging:
        # 2. Convert to JPEG in a throwaway dir: the aspect check runs here, so dry_run catches it too.
        jpeg = Path(staging) / "post.jpg"
        width, height = prepare_image(source, jpeg)
        summary = {
            "username": username,
            "post_type": post_type,
            "media_file": source.name,
            "width": width,
            "height": height,
            "caption_chars": len(text),
            "episode_slug": slug,
        }
        if dry_run:
            return {"status": "dry_run", **summary}

        # 3. Never post the same image with the same caption twice; then stage the JPEG where Meta can fetch it.
        if existing := _logged_post(source_sha256, text):
            return {"status": "exists", **summary, **existing}
        image_url = _stage_on_r2(jpeg, source_sha256)

    # 4. Meta's two steps: a container from the public URL, then publishing it once it's processed.
    access_token = token["access_token"]
    container = _call(f"{token['user_id']}/media", access_token, {"image_url": image_url, "caption": text})
    _wait_until_finished(container["id"], access_token)
    try:
        media_id = _call(f"{token['user_id']}/media_publish", access_token, {"creation_id": container["id"]})["id"]
    except InstagramError as exc:
        # A timeout here leaves the outcome unknown, so the agent must look before posting again.
        raise InstagramError(f"{exc} The post may have gone through: check @{username} before retrying.") from exc

    result: dict[str, Any] = {
        "status": "posted",
        "media_id": media_id,
        # The post is already live; a missing link must not turn success into an error an agent would retry.
        "url": _call_or_empty(f"{media_id}?fields=permalink", access_token).get("permalink"),
        **summary,
    }
    # The post is already live here. A log failure must not raise, or an agent may retry and post twice.
    record = {
        "posted_at": datetime.now(UTC).isoformat(),
        **result,
        "caption": text,
        "media_sha256": source_sha256,
        "media_urls": [image_url],
    }
    if log_error := append_record(settings.instagram_post_log_path, record):
        result["log_error"] = log_error
    return result


def _post_summary(post: dict[str, Any]) -> dict[str, Any]:
    """The fields of an Instagram post an agent or human needs to recognize it, with the caption trimmed."""
    return {
        "media_id": str(post.get("id", "")),
        "type": f"{post.get('media_product_type', 'FEED')}/{post.get('media_type', '')}".lower(),
        "posted_at": post.get("timestamp"),
        "url": post.get("permalink"),
        "caption": (post.get("caption") or "")[:CAPTION_PREVIEW_CHARS],
    }


def list_posts(limit: int = 25, after: str | None = None) -> dict[str, Any]:
    """One page of the account's posts, newest first, and the cursor for the next page (None on the last one)."""
    token = load_token()
    query = {"fields": POST_FIELDS, "limit": max(1, min(limit, MAX_LIST_LIMIT)), **({"after": after} if after else {})}
    page = _call(f"{token['user_id']}/media?{urlencode(query)}", token["access_token"])
    # Meta includes a `next` link only when another page exists; the cursor alone is enough to ask for it.
    paging = page.get("paging", {})
    return {
        "username": token["username"],
        "posts": [_post_summary(post) for post in page.get("data", [])],
        "next_cursor": paging.get("cursors", {}).get("after") if paging.get("next") else None,
    }


def delete_post(media_id: str, dry_run: bool = False) -> dict[str, Any]:
    """Permanently delete one of the account's posts and log what it was; `dry_run` only shows what would go.

    The post is read first, so a wrong id, or a post from another account, is refused
    before anything is deleted. Carousels go as a whole; ads can't be deleted.
    """
    if not _MEDIA_ID_RE.fullmatch(media_id):
        raise ValueError(f"{media_id!r} isn't an Instagram media id (digits only).")
    token = load_token()
    post = _call(f"{media_id}?fields={POST_FIELDS}", token["access_token"])
    owner = str(post.get("username") or "")
    if owner.lower() != token["username"].lower():
        raise InstagramError(
            f"Post {media_id} belongs to {f'@{owner}' if owner else 'an unknown account'}, "
            f"not @{token['username']}; not deleted."
        )
    summary = _post_summary(post)
    if dry_run:
        return {"status": "dry_run", **summary}

    reply = _call(media_id, token["access_token"], method="DELETE")
    if not reply.get("success"):
        raise InstagramError(f"Instagram didn't confirm deleting post {media_id}: {reply}"[:MAX_ERROR_CHARS])
    result: dict[str, Any] = {"status": "deleted", **summary}
    # The post is already gone; a log failure is reported, never raised, and the full caption is kept as the record.
    record = {"deleted_at": datetime.now(UTC).isoformat(), **result, "caption": post.get("caption") or ""}
    if log_error := append_record(settings.instagram_deletion_log_path, record):
        result["log_error"] = log_error
    return result


def clean_pasted_token(raw: str) -> str:
    """The token as pasted, minus whitespace, line breaks, and terminal paste markers."""
    return _PASTE_NOISE_RE.sub("", raw)


def _choose_account(accounts: list[dict[str, str]]) -> dict[str, str]:
    """Pick the account a new token should post to; raise InstagramError with the reason when none fits.

    INSTAGRAM_USERNAME decides when it's set; otherwise the token must reach exactly one
    account, so posting never starts out pointed at a guess.
    """
    if not accounts:
        raise InstagramError(
            "The token works but reaches no Instagram account. A user token reaches the accounts linked to Pages it "
            "manages only with pages_show_list (and business_management for Pages in a business portfolio); a "
            "Unified login token only reaches accounts its system user was given."
        )
    # An account without its id, username, or a token to post with can't be chosen.
    if incomplete := [a for a in accounts if not all(a.values())]:
        raise InstagramError(
            f"Meta listed {len(accounts)} account(s), {len(incomplete)} without an id, username, or Page token; check "
            "that the token has instagram_basic and pages_read_engagement, then generate a new one."
        )
    reached = ", ".join(f"@{a['username']}" for a in accounts)
    if settings.instagram_username:
        wanted = settings.instagram_username.lower()
        if account := next((a for a in accounts if a["username"].lower() == wanted), None):
            return account
        raise InstagramError(f"This token reaches {reached}, not INSTAGRAM_USERNAME @{settings.instagram_username}.")
    if len(accounts) > 1:
        raise InstagramError(
            f"This token reaches {reached}; set INSTAGRAM_USERNAME to the one to post as, then run login again."
        )
    return accounts[0]


def login() -> int:
    """Save the token that posts to the Instagram account, after checking which account the pasted token reaches.

    A pasted user token is never stored: the token saved is the Page token Meta returns for it.
    """
    print("Paste a long-lived user token from Graph API Explorer (see ADOPTING.md, Posting to Instagram),")
    print("a Page token, or a Unified login token. It is read without echoing.")
    value = clean_pasted_token(getpass("Access token: "))
    if not value:
        print("No token entered; nothing saved.", file=sys.stderr)
        return 1
    try:
        accounts = reachable_accounts(value)
    except InstagramError as exc:
        # Length and prefix help tell a truncated or wrong-kind paste apart without revealing the token.
        kind = "starts" if value.startswith(GRAPH_TOKEN_PREFIX) else "does not start"
        print(
            f"The token didn't work, so nothing was saved: {exc}\n"
            f"The pasted value is {len(value)} characters and {kind} with EAA; Page and Unified login tokens start "
            "with EAA. Copy it again with the copy button.",
            file=sys.stderr,
        )
        return 1
    try:
        account = _choose_account(accounts)
    except InstagramError as exc:
        print(f"{exc} Nothing was saved.", file=sys.stderr)
        return 1
    _save_token(account)
    print(f"Saved to {settings.instagram_token_path.expanduser()} (mode 600) for @{account['username']}.")
    if not settings.instagram_username:
        print(f"Set INSTAGRAM_USERNAME={account['username']} in .env to turn posting on.")
    return 0


def main(argv: list[str]) -> int:
    """`login`: save a token from a hidden prompt."""
    if argv != ["login"]:
        print("Usage: uv run python -m exporters.instagram login", file=sys.stderr)
        return 2
    return login()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
