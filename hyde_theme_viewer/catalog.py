"""Theme catalog backed by the HyDE-Project/hyde-gallery repository (the
same database `hyde-shell theme.import` uses). Everything is fetched
through the GitHub REST Contents API and raw.githubusercontent.com, so
browsing never needs a local git clone -- only applying a theme does,
which shells out to HyDE's own theme.patch.sh."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "hyde-theme-viewer"
IMAGE_CACHE_DIR = CACHE_ROOT / "images"
CATALOG_CACHE_FILE = CACHE_ROOT / "hyde-themes.json"

GALLERY_OWNER = "HyDE-Project"
GALLERY_REPO = "hyde-gallery"
GALLERY_JSON_PATH = "hyde-themes.json"

THEME_PATCH_SH = Path(os.environ.get("LIB_DIR", HOME / ".local" / "lib")) / "hyde" / "theme.patch.sh"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
USER_AGENT = "hyde-theme-viewer"


def _discover_github_token() -> str | None:
    """Unauthenticated GitHub API calls are capped at 60/hour, which this
    app can burn through quickly (a tree fetch per repo). Prefer an
    explicit env var, then fall back to the user's own `gh` CLI login
    (5000/hour) if there is one -- no new credentials to manage."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


GITHUB_TOKEN = _discover_github_token()


class GitHubError(RuntimeError):
    pass


def _api_request(url: str) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise GitHubError(f"{url} -> HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"{url} -> {exc.reason}") from exc


def _contents_url(owner: str, repo: str, path: str, branch: str | None = None) -> str:
    quoted = "/".join(urllib.parse.quote(seg) for seg in path.split("/") if seg)
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{quoted}"
    if branch:
        url += f"?ref={urllib.parse.quote(branch)}"
    return url


_dir_cache: dict[tuple[str, str, str, str | None], list[dict] | None] = {}


def _list_dir(owner: str, repo: str, path: str, branch: str | None = None) -> list[dict] | None:
    """One folder's immediate entries, or None if that path doesn't exist.
    Raises GitHubError for a genuine fetch failure (rate limit, network),
    as opposed to a 404 which just means "nothing there"."""
    key = (owner, repo, path, branch)
    if key in _dir_cache:
        return _dir_cache[key]
    try:
        raw = _api_request(_contents_url(owner, repo, path, branch))
    except GitHubError as exc:
        if "HTTP 404" in str(exc):
            _dir_cache[key] = None
            return None
        raise
    data = json.loads(raw)
    entries = data if isinstance(data, list) else None  # a dict back means `path` is a file, not a dir
    _dir_cache[key] = entries
    return entries


def find_images_under(
    owner: str, repo: str, root: str,
    exclude_dirs: set[str] = frozenset({"logo"}), branch: str | None = None,
) -> list[dict]:
    """Every image file anywhere below `root` in the repo (any subfolder
    name, any depth -- mirrors theme.patch.sh's own `find $FAV_THEME_DIR`),
    skipping any path that runs through one of `exclude_dirs`. Walks one
    small directory listing at a time instead of pulling the whole repo's
    tree, so a big unrelated repo doesn't make this slow. Raises
    GitHubError on a genuine fetch failure (rate limit, network) so callers
    can tell "couldn't check" apart from "checked, nothing there"."""
    items: list[dict] = []
    stack = [root]
    while stack:
        path = stack.pop()
        entries = _list_dir(owner, repo, path, branch)
        if not entries:
            continue
        for entry in entries:
            entry_type = entry.get("type")
            name = entry.get("name", "")
            if entry_type == "dir":
                if name not in exclude_dirs:
                    stack.append(entry["path"])
            elif (
                entry_type == "file"
                and Path(name).suffix.lower() in IMAGE_EXTS
                and entry.get("download_url")
            ):
                items.append({"name": name, "download_url": entry["download_url"]})
    return items


@dataclass
class ThemeEntry:
    name: str
    link: str
    owner: str = ""
    description: str = ""
    colorscheme: list[str] = field(default_factory=list)
    repo_owner: str = field(init=False)
    repo_name: str = field(init=False)
    repo_branch: str | None = field(init=False)

    def __post_init__(self) -> None:
        path = re.sub(r"^https?://[^/]+/", "", self.link.rstrip("/"))
        branch = None
        if "/tree/" in path:
            path, branch = path.split("/tree/", 1)
        parts = path.split("/", 1)
        self.repo_owner = parts[0]
        self.repo_name = parts[1] if len(parts) > 1 else parts[0]
        self.repo_branch = branch


def fetch_catalog(use_cache_on_failure: bool = True) -> list[ThemeEntry]:
    try:
        raw = _api_request(_contents_url(GALLERY_OWNER, GALLERY_REPO, GALLERY_JSON_PATH))
        content_b64 = json.loads(raw)["content"]
        data = json.loads(base64.b64decode(content_b64))
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        CATALOG_CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except (GitHubError, KeyError, ValueError) as exc:
        if use_cache_on_failure and CATALOG_CACHE_FILE.exists():
            data = json.loads(CATALOG_CACHE_FILE.read_text(encoding="utf-8"))
        else:
            raise GitHubError(f"Could not fetch theme catalog: {exc}") from exc

    entries = [
        ThemeEntry(
            name=item["THEME"],
            link=item["LINK"],
            owner=item.get("OWNER", ""),
            description=item.get("DESCRIPTION", ""),
            colorscheme=item.get("COLORSCHEME", []),
        )
        for item in data
    ]
    entries.sort(key=lambda e: e.name.lower())
    return entries


def fetch_screenshots(entry: ThemeEntry) -> list[dict]:
    """Preview images from the hyde-gallery repo itself (folder named after the theme)."""
    return find_images_under(GALLERY_OWNER, GALLERY_REPO, entry.name, exclude_dirs=frozenset())


def theme_repo_path(theme_name: str) -> str:
    """Root of a theme's own files -- identical across every theme repo.
    What's inside (wallpapers/, wallpaper/, images loose in this folder, ...)
    varies per author, so wallpaper lookup walks everything under here."""
    return f"Configs/.config/hyde/themes/{theme_name}"


def fetch_wallpapers(entry: ThemeEntry) -> list[dict]:
    """Wallpaper-like images bundled in the theme's own repo. Folder naming
    isn't standardized (wallpapers/, wallpaper/, straight in the theme
    folder, ...) so this mirrors theme.patch.sh: any image anywhere under
    the theme's folder, except under a logo/ subfolder."""
    return find_images_under(
        entry.repo_owner, entry.repo_name, theme_repo_path(entry.name), branch=entry.repo_branch
    )


def cached_image_path(theme_name: str, download_url: str) -> Path:
    safe_theme = re.sub(r"[^\w.-]", "_", theme_name)
    filename = Path(urllib.parse.urlparse(download_url).path).name
    return IMAGE_CACHE_DIR / safe_theme / filename


def download_image(download_url: str, dest: Path) -> Path | None:
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = _api_request(download_url)
    except GitHubError:
        return None
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(raw)
    tmp.rename(dest)
    return dest


def _default_branch(owner: str, repo: str) -> str:
    raw = _api_request(f"https://api.github.com/repos/{owner}/{repo}")
    return json.loads(raw).get("default_branch", "main")


def apply_theme(entry: ThemeEntry) -> tuple[bool, str]:
    if not THEME_PATCH_SH.is_file():
        return False, f"{THEME_PATCH_SH} not found; is HyDE installed?"
    # theme.patch.sh resolves a branch-less URL by shelling out to an
    # *unauthenticated* `curl .../branches`, which is a completely separate
    # quota from our own token-backed requests and gets exhausted easily.
    # Resolving the branch ourselves and passing .../tree/<branch> makes
    # theme.patch.sh skip that call (and its interactive branch prompt)
    # entirely.
    url = entry.link
    if not entry.repo_branch:
        try:
            branch = _default_branch(entry.repo_owner, entry.repo_name)
            url = f"{entry.link.rstrip('/')}/tree/{branch}"
        except GitHubError:
            pass  # fall back to theme.patch.sh's own resolution
    cmd = ["bash", str(THEME_PATCH_SH), entry.name, url]
    proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    ok = proc.returncode == 0
    output = (proc.stdout + proc.stderr).strip()
    return ok, output
