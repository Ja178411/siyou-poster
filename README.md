# siyou-poster

Cloud-scheduled SIYOU Instagram + Facebook posting via Claude remote agent.

## Files

- `briefs.json` — source of truth: 24 batches with R2 video/image URLs, captions A/B/C, products
- `posted.json` — state: which batches have been published, last variant used
- `update_briefs.py` — local script to refresh briefs.json from `~/Downloads/SIYOU_Content_2026-04-28/`

## How it works

A Claude scheduled routine runs daily at 7pm ET (23:00 UTC). It:

1. Fetches `briefs.json` and `posted.json` from this repo
2. Picks the next unposted **taggable** batch (newest first, skipping non-Shopify SKUs)
3. Picks the next caption variant (rotates A → B → C)
4. Skips dups within 14 days (prevents posting same product twice quickly)
5. Posts to Instagram via Meta Graph API:
   - Video → resumable upload not needed since R2 URLs are public; uses `video_url`
   - Image carousel → standard `is_carousel_item` flow
   - Tags products via `product_tags` parameter
6. Posts to Facebook Page (no product tagging — API limitation)
7. Commits updated `posted.json` back to this repo
8. Sends Gmail notification with post URLs

## Manual operations

Re-run posting now:
```
Trigger the routine via https://claude.ai/code/routines/<ROUTINE_ID>
```

Refresh briefs.json (after new batches arrive):
```bash
cd ~/Desktop/meta-posting-mcp
uv run python upload_and_index.py
cp briefs.json ~/Desktop/siyou-poster/briefs.json
cd ~/Desktop/siyou-poster
git add briefs.json && git commit -m "refresh briefs" && git push
```

## Constraints

- Meta API v25.0
- IG: max 50 publishes per 24h
- Carousel: max 20 product tags total, max 5 per slide
- Reels: max 30 product tags
- DST: when EST resumes (Nov 2026), update routine cron from `0 23 * * *` to `0 0 * * *` (6pm becomes 11pm UTC)

## SKUs not yet listed on Shopify (skip until added)

- Batch 088 (M015), 098 (MJS-02912), 101 (MJS-02605), 104 (D560)
- Batch 093 has 02946 missing (companion 03008 is fine)
