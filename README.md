# Prijstracker — lowest furniture prices since a start date

A **static GitHub Pages** site that tracks the lowest price of each item since the
tracking start date. Prices are refreshed **automatically every day** by a
**GitHub Actions** job that runs a real headless browser (Playwright) in the cloud,
extracts each price, and commits the update.

- **No API tokens.** The job commits with GitHub's built-in `GITHUB_TOKEN`.
- **No local tools needed.** You don't need git, Python or Node on your PC — the
  scraping runs on GitHub's servers. Everything below is done in the GitHub website.
- **On-demand refresh.** A "Run workflow" button re-checks prices whenever you want.
- **Manual fallback via chat.** If a shop ever blocks the cloud runner, ask Claude in
  chat and it will fetch live prices and hand you an updated `data/prices.json`.

```
price-tracker/
├── index.html                       ← the dashboard (open this URL)
├── data/config.json                 ← YOUR ITEM LIST (edit to add/remove items)
├── data/prices.json                 ← tracked prices + history (auto-updated)
├── scraper/scrape.py                ← price extractor (runs in the cloud)
├── scraper/requirements.txt
└── .github/workflows/track-prices.yml
```

---

## One-time setup (≈5 minutes, all in the browser)

1. **Create a GitHub account** if you don't have one — <https://github.com/signup>.

2. **Create a new repository.** Top-right **+ → New repository**.
   - Name: e.g. `price-tracker`
   - Visibility: **Public** *(required for free GitHub Pages + unlimited Actions minutes; the page only shows furniture prices, nothing private)*
   - Click **Create repository**.

3. **Upload these files.** On the empty repo page: **Add file → Upload files**.
   Drag the **entire contents** of the `price-tracker` folder in
   (`index.html`, the `data`, `scraper`, and `.github` folders). The folder
   structure is preserved. Click **Commit changes**.
   > If the `.github` folder doesn't appear after dragging, use **Add file →
   > Create new file**, type `​.github/workflows/track-prices.yml` as the name
   > (the slashes create the folders), paste the file's contents, and commit.

4. **Give the Action permission to commit.**
   **Settings → Actions → General → Workflow permissions →** select
   **"Read and write permissions" → Save.**

5. **Turn on GitHub Pages.**
   **Settings → Pages → Build and deployment → Source: "Deploy from a branch" →**
   Branch **`main`**, folder **`/ (root)` → Save.**
   After ~1 minute your page is live at:
   **`https://<your-username>.github.io/price-tracker/`**

6. **Do the first price refresh.**
   **Actions** tab → **Track prices** (left) → **Run workflow → Run workflow**.
   Watch it install Chromium, scrape, and commit. Reload your page — prices appear.
   *(After this it runs by itself every day around 08:00 Brussels time.)*

---

## Everyday use

**Refresh prices now:** Actions → *Track prices* → **Run workflow**. (It also runs daily.)

**Add or remove an item:** edit **`data/config.json`** in the GitHub web editor
(pencil icon), add/remove an entry, commit. The next run picks it up — new items
start tracking fresh; removed items disappear from the page.

```jsonc
{
  "id": "uniek-kort-id",          // any unique slug
  "category": "TV MEUBEL",        // becomes a section heading on the page
  "name": "Shop · Productnaam",   // shown on the card
  "url": "https://…"              // the product page
}
```

**Restart the "lowest since" baseline:** change `trackingStartDate` in
`data/config.json`, and (optionally) trim each item's `history` in
`data/prices.json` to a single current point.

**Manual refresh through chat (fallback):** if an item stays stale, tell Claude
which ones; it fetches the live prices and gives you a replacement
`data/prices.json` to upload via the web editor.

---

## How "lowest price" works

`scrape.py` records a history point whenever a price **changes**. The card's
**"Laagste sinds start"** value is the minimum across all recorded points since
`trackingStartDate`, with the date it was first seen. The green
**"Laagste tot nu toe"** badge means the current price equals that minimum.

For configurable La Redoute products the tracked figure is the **"vanaf" (from)**
price — the cheapest variant.

---

## Alerts — email when a new record-low hits

When a run finds a price **below an item's previously recorded lowest**, the
scraper writes `ALERT_BODY.md` and the workflow opens a **GitHub Issue that
@mentions you** — so GitHub emails you. No tokens, no third-party service.

- To receive the mail: be signed in to GitHub with email notifications on
  (**Settings ▸ Notifications ▸ Email** — enable issue/participating emails).
- Alerts fire **only on a genuine new low** — not on the first price, and not on
  equal or higher prices — so they stay rare and meaningful.
- The issue lists item, old lowest, new price and the drop %. Close it once read.

## Page password

The page is gated by a password — **`hunter2`** — checked client-side (SHA-256).
Change it by replacing the `PW_HASH` value in `index.html` with the SHA-256 of a
new password:

```bash
python -c "import hashlib; print(hashlib.sha256(b'NEWPASS').hexdigest())"
```

> ⚠️ **Light obfuscation, not real security.** A public GitHub Pages repo means
> `data/prices.json` is reachable directly and the JS gate can be bypassed by
> anyone technical. For *real* protection, ask me to **encrypt the price data with
> your password** (decrypted in the browser) — then the password actually guards
> the contents.

## Good to know / limitations

- **Bot protection.** La Redoute and fonQ block simple HTTP clients, which is why
  the scraper uses a real Chromium. It passes their checks in normal cases, but a
  cloud datacenter IP can occasionally get challenged. When that happens the item
  is flagged **stale** and **keeps its last known price** (history is never
  corrupted). Re-running usually fixes it; chat is the guaranteed fallback.
- **fonQ "Glinta" currently 404s** for automated fetchers, so it starts as
  *pending* until a scrape succeeds. If it stays pending, the product may be
  delisted — replace its `url` in `config.json`.
- **Scheduled workflows pause after 60 days of repo inactivity** (a GitHub rule).
  If the daily run ever stops, click **Run workflow** once (or edit any file) to
  wake it back up.
- **Initial prices** (€899 / €679 / €699 / €449 / €54,99) were verified on
  2026-06-22; fonQ was not reachable and is pending.
