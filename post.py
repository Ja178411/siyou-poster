"""SIYOU daily IG + FB poster, runs in GitHub Actions."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

API = "v25.0"
GRAPH = f"https://graph.facebook.com/{API}"
IG_USER = "17841476776530806"
PAGE_ID = "756528474221592"

ROOT = Path(__file__).parent
BRIEFS = ROOT / "briefs.json"
POSTED = ROOT / "posted.json"

META_TOKEN = os.environ["META_ACCESS_TOKEN"]
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def log(*args):
    print(f"[{datetime.now().isoformat(timespec='seconds')}]", *args, flush=True)


def fetch_recent_ig_captions(client: httpx.Client) -> list[str]:
    r = client.get(
        f"{GRAPH}/{IG_USER}/media",
        params={"access_token": META_TOKEN, "fields": "caption,timestamp", "limit": 50},
        timeout=30,
    )
    r.raise_for_status()
    return [norm(item.get("caption", "")) for item in r.json().get("data", [])]


def is_already_posted(batch: dict, recent_captions: list[str], posted_state: dict) -> bool:
    if any(p["batch"] == batch["batch"] for p in posted_state["posted"]):
        return True
    needles = [norm(batch["captions"].get(v, ""))[:60] for v in ("A", "B", "C")]
    needles = [n for n in needles if n]
    for cap in recent_captions:
        for needle in needles:
            if needle and needle in cap:
                return True
    return False


def has_caption(batch: dict) -> bool:
    caps = batch.get("captions") or {}
    return any((caps.get(v) or "").strip() for v in ("A", "B", "C"))


def pick_next(briefs: dict, posted_state: dict, recent_captions: list[str]) -> dict | None:
    candidates = [
        b for b in briefs["batches"]
        if b.get("taggable")
        and has_caption(b)
        and not is_already_posted(b, recent_captions, posted_state)
    ]
    candidates.sort(key=lambda b: int(b["batch"]), reverse=True)
    return candidates[0] if candidates else None


def next_variant(last: str | None) -> str:
    return {"A": "B", "B": "C", "C": "A"}.get(last or "C", "A")


def page_token(client: httpx.Client) -> str:
    r = client.get(f"{GRAPH}/me/accounts", params={"access_token": META_TOKEN, "fields": "id,access_token"}, timeout=30)
    r.raise_for_status()
    for p in r.json().get("data", []):
        if p["id"] == PAGE_ID:
            return p.get("access_token") or META_TOKEN
    return META_TOKEN


def wait_container(client: httpx.Client, cid: str, timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(
            f"{GRAPH}/{cid}",
            params={"access_token": META_TOKEN, "fields": "status_code,status"},
            timeout=30,
        )
        body = r.json()
        code = body.get("status_code")
        log(f"  container {cid} status: {code}")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Container {cid} failed: {body}")
        time.sleep(5)
    raise TimeoutError(f"Container {cid} did not finish in {timeout}s")


def prepare_reel(client: httpx.Client, video_url: str) -> Path:
    """Download the source video and re-encode it to Meta Reels-compliant specs.

    Why: Meta rejects MP4s without faststart (moov before mdat) and recommends
    video bitrate <=5 Mbps with AAC 128 kbps / 48 kHz. Source videos in R2 fail
    both, so we transcode in CI before uploading.
    """
    src = Path(tempfile.mkstemp(suffix=".mp4", prefix="reel_src_")[1])
    log(f"Downloading {video_url}")
    with client.stream("GET", video_url, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with src.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
    log(f"  downloaded {src.stat().st_size:,} bytes")

    dst = src.with_name(src.stem + ".reels.mp4")
    log("Re-encoding to Reels spec (H.264 high@4.0, 4.5/5 Mbps, AAC 128k/48k, faststart)")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-c:v", "libx264", "-profile:v", "high", "-level", "4.0", "-preset", "veryfast",
            "-b:v", "4500k", "-maxrate", "5000k", "-bufsize", "10000k",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(dst),
        ],
        check=True,
    )
    src.unlink(missing_ok=True)
    log(f"  re-encoded: {dst.stat().st_size:,} bytes")
    return dst


def initiate_reel_resumable(client: httpx.Client, caption: str) -> tuple[str, str]:
    r = client.post(
        f"{GRAPH}/{IG_USER}/media",
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true",
            "access_token": META_TOKEN,
        },
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    return body["id"], body["uri"]


def upload_reel_bytes(client: httpx.Client, upload_uri: str, file_path: Path) -> None:
    size = file_path.stat().st_size
    log(f"Uploading {size:,} bytes to Meta resumable endpoint")
    with file_path.open("rb") as f:
        r = client.post(
            upload_uri,
            headers={
                "Authorization": f"OAuth {META_TOKEN}",
                "offset": "0",
                "file_size": str(size),
                "Content-Type": "application/octet-stream",
            },
            content=f.read(),
            timeout=600,
        )
    r.raise_for_status()
    log(f"  upload response: {r.json()}")


def post_video_ig(client: httpx.Client, file_path: Path, caption: str, max_attempts: int = 2) -> str:
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        log(f"Initiating IG REELS resumable upload (attempt {attempt}/{max_attempts})")
        cid, upload_uri = initiate_reel_resumable(client, caption)
        log(f"  container_id={cid}")
        try:
            upload_reel_bytes(client, upload_uri, file_path)
            wait_container(client, cid, timeout=300)
        except (RuntimeError, httpx.HTTPError) as e:
            log(f"  attempt {attempt} failed: {e}")
            last_err = e
            if attempt < max_attempts:
                time.sleep(15)
                continue
            raise
        pub = client.post(
            f"{GRAPH}/{IG_USER}/media_publish",
            data={"creation_id": cid, "access_token": META_TOKEN},
            timeout=60,
        )
        pub.raise_for_status()
        media_id = pub.json()["id"]
        log(f"  IG published: {media_id}")
        return media_id
    raise RuntimeError(f"All {max_attempts} attempts failed: {last_err}")


def post_carousel_ig(client: httpx.Client, target: dict, caption: str) -> str:
    log(f"Creating IG CAROUSEL with {len(target['image_urls'])} images")
    child_ids: list[str] = []
    for url in target["image_urls"]:
        r = client.post(
            f"{GRAPH}/{IG_USER}/media",
            data={"image_url": url, "is_carousel_item": "true", "access_token": META_TOKEN},
            timeout=60,
        )
        r.raise_for_status()
        child_ids.append(r.json()["id"])
    for cid in child_ids:
        wait_container(client, cid, timeout=120)
    parent = client.post(
        f"{GRAPH}/{IG_USER}/media",
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "caption": caption,
            "access_token": META_TOKEN,
        },
        timeout=60,
    )
    parent.raise_for_status()
    pid = parent.json()["id"]
    wait_container(client, pid, timeout=120)
    pub = client.post(
        f"{GRAPH}/{IG_USER}/media_publish",
        data={"creation_id": pid, "access_token": META_TOKEN},
        timeout=60,
    )
    pub.raise_for_status()
    media_id = pub.json()["id"]
    log(f"  IG carousel published: {media_id}")
    return media_id


def tag_products(client: httpx.Client, media_id: str, product_ids: list[str]) -> None:
    if not product_ids:
        return
    log(f"Tagging {len(product_ids)} products on {media_id}")
    payload = json.dumps([{"product_id": pid} for pid in product_ids])
    r = client.post(
        f"{GRAPH}/{media_id}/product_tags",
        data={"access_token": META_TOKEN, "updated_tags": payload},
        timeout=60,
    )
    r.raise_for_status()
    log(f"  tags applied: {r.json()}")


def post_fb(client: httpx.Client, target: dict, caption: str) -> str:
    token = page_token(client)
    if target.get("media_type") == "CAROUSEL":
        url = target["image_urls"][0]
        log(f"Posting first image of carousel to FB: {url}")
        r = client.post(
            f"{GRAPH}/{PAGE_ID}/photos",
            data={"url": url, "message": caption, "access_token": token},
            timeout=60,
        )
        r.raise_for_status()
        return r.json().get("post_id") or r.json().get("id", "")
    log(f"Posting video to FB: {target['video_url']}")
    r = client.post(
        f"{GRAPH}/{PAGE_ID}/videos",
        data={"file_url": target["video_url"], "description": caption, "access_token": token},
        timeout=180,
    )
    r.raise_for_status()
    return r.json().get("id", "")


def main() -> int:
    briefs = json.loads(BRIEFS.read_text())
    posted_state = json.loads(POSTED.read_text())

    with httpx.Client() as client:
        recent = fetch_recent_ig_captions(client)
        log(f"Fetched {len(recent)} recent IG captions")

        target = pick_next(briefs, posted_state, recent)
        if not target:
            log("No taggable unposted batches available — queue empty")
            return 0

        variant = next_variant(posted_state.get("last_variant"))
        caption = target["captions"].get(variant) or target["captions"].get("A", "")
        if not caption:
            log(f"ERROR: no caption found for batch {target['batch']}")
            return 1

        log(f"Selected batch {target['batch']} ({', '.join(target['codes'])}), variant {variant}")
        log(f"Products: {', '.join(p['shopify_title'] for p in target['products'])}")
        log(f"Caption preview: {caption[:80]}...")

        if DRY_RUN:
            log("DRY_RUN=1 — skipping actual posts")
            return 0

        local_video: Path | None = None
        try:
            if target.get("media_type") == "CAROUSEL":
                ig_id = post_carousel_ig(client, target, caption)
            else:
                local_video = prepare_reel(client, target["video_url"])
                ig_id = post_video_ig(client, local_video, caption)

            product_ids = [p["ig_product_id"] for p in target["products"] if p.get("ig_product_id")]
            try:
                tag_products(client, ig_id, product_ids)
            except Exception as e:
                log(f"  Tag application failed (post still up): {e}")

            try:
                fb_id = post_fb(client, target, caption)
            except Exception as e:
                log(f"  FB post failed (IG already up): {e}")
                fb_id = ""
        finally:
            if local_video is not None:
                local_video.unlink(missing_ok=True)

    posted_state["posted"].insert(0, {
        "batch": target["batch"],
        "code": ",".join(target["codes"]),
        "ig_media_id": ig_id,
        "fb_video_id": fb_id,
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "variant": variant,
        "products_tagged": product_ids,
    })
    posted_state["last_variant"] = variant
    POSTED.write_text(json.dumps(posted_state, indent=2) + "\n")
    log(f"Done. IG={ig_id}, FB={fb_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
