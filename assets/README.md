# Assets

| File | What it is | Used by |
| --- | --- | --- |
| `background.jpg` | The gothic plate (2048×2048) | Full-page background layer behind every section |
| `logo.png` | The chrome XR mark, 900×617 | Pinned hero mark |
| `favicon.png` | 256×256 crop of the logo | Browser tab |
| `social/instagram.svg` | Instagram brand mark | Top bar |
| `social/pinterest.svg` | Pinterest brand mark | Top bar |
| `social/discord.svg` | Discord brand mark | Top bar (dimmed — no invite yet) |
| `creators/chokinglotus.jpg` | Creator avatar, shipped copy | Creator row |
| `creators/numen.jpg` | Creator avatar, shipped copy | Creator row |
| `creators/g59.jpg` | Creator avatar, shipped copy | Creator row |
| `creators/scissors.jpg` | Creator avatar, shipped copy | Creator row |
| `creators.json` | Creator id → live IMVU picture | Reference for the avatar URLs |
| `products.json` | The IMVU shop's eight newest items, newest first | Rendered into the releases row at load |
| `status.json` | The creator's presence label | Top bar — the status dot and its word |
| `notification.txt` | The line the notice strip types out | Notice strip under the top bar |

`logo.png` is processed: the dead black border was cropped and the black background keyed to transparency, so it drops onto the plate with no visible rectangle and no blend mode. In the hero it is sized to the plate's own circle and centred on it, so the ink sits just inside the arc.

## Creator status

`tools/track-status.py` keeps it current: it reads IMVU's public user record for the XR member (`api.imvu.com/user/user-106440837`), checks the username really is `xr`, and writes `Online` or `Offline`, so the dot reflects the account rather than someone's memory. A Windows scheduled task, **XR status refresh**, runs it as `--watch 30`: one long-lived process that asks IMVU twice a minute. Windows Task Scheduler cannot trigger more than once a minute, so the task's own repeat is only the restart net — while the watcher runs, Windows counts the task as running and starts nothing. Changes are logged to `%TEMP%\xr-status.log`, and a failed read leaves the last known label in place. Every poll is also written to `%TEMP%\xr-availability.log` — one line per look with the `source` (`public` or `override`), the `online` flag, the label it produced and the confirm streak — rotating at 256 KB into `.1`. It records the uninteresting samples as well as the changes, so a reading that quietly stops moving is visible there. The page re-reads `status.json` every 30 seconds, so the dot changes without a reload and lags the account by at most about a minute.

**The label is only ever Online or Offline.** IMVU's client modes — "Creating", "Do Not Disturb" — are published only to a signed-in visitor, by the avatar card at `avatars.imvu.com/api/avatarcard.php`; the anonymous record leaves `availability` empty whatever mode the client is in, and reading the card would cost a session cookie plus a second request on every poll. No session has ever been available here, so the watcher does not pretend to read them: the card and session code that used to try was removed rather than left dormant, along with its capture tools and tests.

`status.json` is three keys:

```json
{
  "status": "Online",
  "source": "public",
  "updated_at": "2026-11-15T15:35:19Z"
}
```

`source` is `public` for a reading from IMVU, `override` for a hand-set label. The page reads `status`, `manual` and `updated_at` and ignores anything else.

`updated_at` is rewritten on **every** poll, whether or not the label moved, and it is the one key excluded from the change comparison. Without it a watcher that died at nine and one that has said "Offline" all day produce identical files. The page treats a stamp older than three minutes as **stale**: the dot drops to the muted unknown state, the word reads `stale`, and the tooltip says when the last reading was — so a dead task looks dead instead of looking offline.

That diagnostic is only available where the watcher could be running. The plate shows always on loopback or from `file://` (`CAN_READ_LIVE`); anywhere else — GitHub Pages included — it is hidden from the moment the script loads and never shown, whatever `status.json` happens to say.

## Live status on the published site

`worker/` holds that endpoint: a Cloudflare Worker deployed through Sites as **XR status endpoint** (`https://site-2802fe79c2704493878ad5de9a76ce50.freebuff.page/`), redeployed from `worker/deploy.json`. It reads the same public record `tools/track-status.py` reads, checks the username is `xr`, and answers `{status, source, updated_at}` with `Access-Control-Allow-Origin: *` and `no-store`. A failed read answers 502 with an error rather than "Offline" — answering offline for an account nobody could check is the one wrong answer it must not give.

Raw readings are not safe to publish. Measured over a minute, IMVU's flag flipped True/False on nearly every 30s sample while the client was open, so a raw endpoint would make the plate strobe between creating and offline every poll. The Worker therefore applies the watcher's own rules to one row in its D1 database: **two agreeing samples before claiming Online**, and **six minutes of offline before believing it**. Every visitor gets that settled label; the response carries `x-xr-state: settled`, or `degraded` (with the raw label) if the database is unreachable.

The first request into an empty database seeds the label from that single reading, so the very first visitor after a cold start can see the raw state for a poll or two before the confirm/grace rules take over.

To make the plate work on a static host, point the page at a live endpoint. `index.html` carries `<meta name="status-source" content="" />`; leaving it empty means `assets/status.json`, and filling it with a URL that answers the same three keys (`status`, `source`, `updated_at`) makes the plate available there too. On such a build the plate appears while the reading is fresh and hides again if the endpoint stops refreshing it, so a dead endpoint cannot leave it claiming a reading. Nothing else in the page changes; `tools/track-status.py` still writes the local file. Showing it there just for the few minutes a freshly committed stamp looks fresh was worse than hiding it: a visitor would watch the plate claim a reading and then disappear. A static host serves `status.json` as a frozen file, so its stamp is always minutes or hours old, and a plate that could only ever say `stale` is worse than no plate at all: it is a broken-looking widget rather than a live one. Hiding it is the honest answer to a host that cannot know. A throttle pause still refreshes the stamp, because the watcher is alive and deliberately holding the last label, and so does a hand-pinned `manual` label; only a poll that never finishes leaves it behind.

The published flag is a heartbeat, not a session: measured against the real account it flipped to offline for minutes at a time while the client was open, which made the dot flash online/offline every few minutes. So the watcher only claims online after two agreeing samples (`ONLINE_CONFIRM`), and only accepts offline once it has outlasted a six-minute grace window (`ONLINE_GRACE`). A genuine log-off therefore darkens the dot up to six minutes late, which is the price of it not flickering.

When IMVU answers 429, or a body saying "too many requests", the watcher stops asking for two minutes (`THROTTLE_SECONDS`) and logs `throttled: IMVU answered 429 … — last label kept` once, leaving the label untouched on every poll in between rather than retrying a rate limit every 30 seconds until it becomes a block. The page itself no longer paints a default reading while `status.json` is being read — that was the flash of a confident dot and then grey on every load.

The page has one rule: a label beginning `off` shows **offline**, in the same muted grey as the footer's "The Art of Immersion" line, holding still. Everything else shows **creating**: this is a creator's page, so being at the desk is the creating state, and the plate wears a **red ring with a transparent centre** under the 1.4s flash. The plate and the `new` markers both draw `--red-ring`, so every red dot on the page is one identical open circle: a 1px ring, a dark middle, the same halo. Their blinking is kept in step too — CSS animations start whenever their element did, and the plate only starts once the status says creating, so the page restarts them together after each status read rather than letting the plate drift against the markers. `status.json` still records the reading as `Online`, because that is what IMVU published; only the plate renames it. The test is one prefix check on the lowercased label, because the watcher writes nothing else — `Offline` or `Online`, never a client mode — so there is no other label to map and no online state in between to fall through to.

Without a session a mode can only be set locally, with `assets/status.override.txt`:

    printf 'Creating\n' > assets/status.override.txt   # shows creating until cleared
    rm assets/status.override.txt                      # back to IMVU's answer

Any label on its first line outranks IMVU while the file is there (the log records it as `(override file)`). A hand-written `"manual": true` in `status.json` is honoured too, and while that flag is set the dot renders **hollow** — a ring instead of a filled dot, with its glow withheld — so a pinned reading never passes for a live one. The override file alone does not set that flag, so its label wears the normal filled dot.

`python tools/test_track_status.py` covers all of it: stdlib only, no network, every path redirected to a temp directory, with the presence read stubbed — the confirm streak, the grace window, a failed read keeping the last label, the stamp moving on every poll and during a pause, the override and manual labels, and a change being logged once. One class takes the stubs away: `RealRateLimit` starts a throwaway server on `127.0.0.1` and answers a real `429`, and a `200` whose body says "too many requests", so the code that reads the status line is exercised over a socket. `ServeDenyList` checks that the preview server refuses cookie files in every encoding.

The watcher runs that suite once before it starts watching and **refuses to start** if it fails: exit code 2, and one log line naming the failing tests (`self-check failed: 3 failing (…) — not starting`). A broken edit therefore cannot go live in the scheduled task — the task simply does not run, `status.json` keeps its last label, and the log says why. A one-off run warns but still runs, since that is how the edit gets debugged; `--skip-self-check` silences the warning. The check is a subprocess rather than an import, so it exercises the file on disk exactly as the task would.

## Page order

Pinned mark → top bar (clock, status, channels) → notice strip → launch panel (creator chips, drop head, countdown, releases grid) → footer.

## Ported from `xr-launch-countdown`

- **Countdown** — the weekly drop, every Friday at local midnight. The target is derived from the clock (`nextDrop()` in `index.html`), so it rolls over to the next Friday the moment a drop opens; the label switches to "Drop day" on the day itself. Change `DROP_WEEKDAY` (5 = Friday, 0 = Sunday) or `DROP_HOUR` to move it.
- **Product tiles** — rendered from `products.json` at load: name, price in credits and the link into the shop, with the rebuilt 300×240 WebP in `assets/products/`. Four cards show at every width; past four the row clones itself and loops.
- **Creators** — `chokinglotus`, `Numen`, `G59` and `scissors`, each linking to their IMVU shop by manufacturer id. The shipped JPEG paints immediately; their current IMVU upload replaces it once it decodes, so the frame never blanks.
- **Socials** — Pinterest and Instagram point at the real accounts. Discord has no invite in the source project either, so it renders dimmed and unclickable.
- **Clock** — `[ HH:MM:SS ]` in the bracket style, from the local system clock.

To change the products by hand, edit `assets/products.json` — newest first. Each entry needs `id`, `name`, `price`, `src` (a file in `assets/products/`) and `url`; the row renders whatever the list holds and the count in the heading follows it. `image` keeps the original CDN URL for reference.

## Product captions

A tile's caption sits on a scrim (`.tile__info::before`), because the artwork above it can be anything from near-black to a bright white prop:

    linear-gradient(180deg, rgba(5,5,8,0) 0%, rgba(5,5,8,0.55) 12%, rgba(5,5,8,0.8) 40%, rgba(5,5,8,0.94) 100%)

The 12% stop is not decoration. A single-line name starts about a quarter of the way down the caption block, but a long one wraps to three lines on a narrow card and reaches within 14% of the top — where a slower ramp left white ink at 4.24:1 on the brightest upload. The 40% stop then carries the rest.

Measured by painting the caption text transparent, screenshotting the rendered page and sampling the background pixels under each line (WCAG relative luminance; the price line composited at its 80% white). Every card carried a deliberately long name so each block wrapped as far as it can:

| width | layout | weakest line | contrast |
| --- | --- | --- | --- |
| 390px | 2 cards, name wraps to 3 lines | first name line | **8.31:1** |
| 600px | 2 cards, 2 lines | first name line | 10.23:1 |
| 620px | breakpoint, 2 cards, 2 lines | first name line | 10.03:1 |
| 640px | 4 cards, 3 lines | first name line | **7.83:1** |
| 1124px | 4 cards, 2 lines over the brightest prop in the artwork | first name line | 9.42:1 |

Body text needs 4.5:1 and white on near-black is what the design wants, so the worst figure anywhere — 7.83:1 at 640px — is the number to keep. Prices measure 10–12:1 throughout, and the median background under a caption stays at or below 0.015 luminance. The first line of a wrapped name is always the weakest point, at any width: if the gradient, the caption padding or the font sizes change, re-measure that line rather than assuming it still clears.

## Adding a creator

`tools/creator-lookup.py` reads a username's IMVU pages for you and prints a card ready to paste into `.creator-list`:

    python tools/creator-lookup.py scissors --save

It prints the member id, the shop link, how many listings that shop holds, the live picture URL and the `<li>` block. `--insert` skips the copy-and-paste: it saves the portrait and drops the card at the end of `.creator-list` in `index.html` (or the file given with `--index`), writing through a temporary file and keeping the page's own line endings. Re-running it for a creator already in the row reports that and leaves the page untouched. `--save` downloads the portrait, centre-crops it to the card's 13:18 frame and writes `assets/creators/<name>.jpg` (104×144) — the shipped copy the other cards use. The picture is taken from the profile's own `avBigPic` block, because the same page also lists other members' thumbnails. If the shop that answers to that id belongs to a different name, the member has no storefront and the script links their profile instead.

## Previewing locally

    python tools/serve.py --supervise   # http://127.0.0.1:4173, restarts itself if it exits

The preview is also registered as a Windows scheduled task, **XR preview server**, which runs that command every minute. It never exits while healthy, so Windows treats it as already running and starts nothing extra; if it ever dies, the next minute's trigger brings it back, and the watchdog inside it covers a crash of the server child within about a second. A second copy asked to serve the same port logs `standing down` and exits rather than fighting over it.

`sw.js` registers a service worker that keeps `offline.html` cached. Once the page has been loaded while the server was up, a navigation that cannot reach the server renders that page — at the same URL, styled like the site — instead of the browser's connection error, and it retries itself every eight seconds. Only navigations are intercepted; assets still come from the server, so nothing stale is served by accident. Bump `CACHE` in `sw.js` when `offline.html` changes.

Stop or remove it with:

    schtasks /end /tn "XR preview server"
    schtasks /delete /tn "XR preview server" /f

Use this rather than `python -m http.server`. It stamps the page it serves with the modification time of the files behind it: the stylesheet link becomes `styles.css?v=<mtime>`, and a `<meta name="products-version">` tag carries `assets/products.json`'s, which the page appends to its own request. So a CSS edit always produces a new URL and the browser cannot show an old sheet — which is exactly how the releases label once appeared left-aligned when the CSS already said otherwise — while the hourly products refresh revalidates one stable URL instead of being re-downloaded every minute. The page is sent `no-store` and assets `no-cache`, so nothing goes stale. Request lines go to `%TEMP%\xr-serve.log`, which rotates at 256 KB into a single `xr-serve.log.1` backup, so the file cannot grow without bound. `index.html` itself keeps a plain `styles.css` link and an empty meta tag, so it still works opened straight from disk or on any static host, where the page falls back to a timestamp.

## Keeping the releases current

IMVU's shopfront is server-rendered and sends no CORS headers, so the page cannot read it directly. `tools/refresh-products.py` reads it instead — it pulls the newest listings for the XR shop (manufacturer `106440837`), rebuilds each thumbnail as a 300×240 WebP and rewrites `products.json` atomically. A Windows scheduled task, **XR product refresh**, runs it hourly, so a new listing reaches the page within the hour (the page re-reads `products.json` every 60s, so it appears as soon as the file changes). Each run appends one line to `%TEMP%\xr-products.log`.

The shop only publishes 100×80 originals — checked against the listing markup, the product page's `resized_image` service (which caps at the source), the API's `product_image` field and the obvious filename variants, all of which return the same 100×80 PNG — so each 300×240 thumbnail is a 3× enlargement, and no resampling can invent detail that was never captured. What can be recovered is edge contrast, and it is applied **before** the resize: sharpening the 100×80 original hands LANCZOS stronger gradients to interpolate, which measured 276 against 190 (variance of the Laplacian, both scaled to the 229px the card actually draws) for a single post-pass, with only faint ringing where a heavy post-pass leaves halos. A light `1.2/80` pass afterwards settles the result. Anything stronger looked crisper in a metric but showed visible halos around the bright silver in the septum pieces, so `PRE_SHARPEN`/`POST_SHARPEN` are deliberately conservative. Thumbnails carry `imageStyle`; bump `THUMB_STYLE` when the pipeline changes so old files rebuild even though their source bytes never moved.

Thumbnails are only rebuilt when the picture behind them actually changes. Each entry records a `imageHash` of the source image (plus `imageEtag`/`imageModified` when the CDN offers them), so a refresh sends a conditional request, gets `304 Not Modified`, and leaves both the file and its timestamp alone — eight files are no longer re-encoded every hour for nothing. A source that does change, a missing thumbnail, or a record with no hash all still force a rebuild, and the run reports `(N images rebuilt)` so a quiet run is visible as such.

    python tools/refresh-products.py
    python tools/refresh-products.py --count 12

Run it on a schedule and the row stays current: the page re-reads `products.json` every minute while it is open and only re-renders when the list actually changed. On Windows, the hourly task (this is the one registered here, named **XR product refresh**, starting at :07):

    schtasks /create /f /tn "XR product refresh" /sc hourly /mo 1 /st 00:07 ^
      /tr "\"C:\path\to\pythonw.exe\" \"%CD%\tools\refresh-products.py\""

On a host rather than a desktop, the same command is the whole cron/Actions step — it needs Python 3 and Pillow.

## Rewriting product descriptions

IMVU publishes no API for descriptions, so `tools/update-product-descriptions.py` drives the creator editor at `https://www.imvu.com/creator/edit_product_html/<id>` — the page you would use by hand. It reads that page's form, keeps every field exactly as found (the CSRF token included), replaces only the description textarea and posts the form back, which is the same thing the Save button does. Both urlencoded and multipart forms are handled, because the editor may carry an image field; file and button inputs are never re-sent.

The editor needs a signed-in session, and does not say so politely: without a cookie it answers **200 with the public IMVU homepage**, no form and no redirect. That is why the script treats "no form on the page" as "not signed in" and stops, rather than reporting a product as done. It checks one product before touching the rest, and exits 2.

The cookie comes from a file — `%LOCALAPPDATA%\XR\session.cookies` by default — holding either a raw `name=value; name=value` string or a whole `Cookie:` header copied from a signed-in browser request. The script never prints it, logs it or writes it into a report, and `.gitignore` covers any stray `*.cookies`.

    python tools/update-product-descriptions.py --copy-from 76546193
    python tools/update-product-descriptions.py --copy-from 76546193 --apply
    python tools/update-product-descriptions.py --description-file new.html --apply
    python tools/update-product-descriptions.py --ids 76546193 76508289 --apply

Without `--apply` it posts nothing; it prints the old and new text per product and stops. With it, each run first copies the description it is about to replace into `%TEMP%\xr-description-backups\<id>.html`, so a bulk edit that goes wrong can be undone by pointing `--description-file` at the backup. `--ids` defaults to every id in `assets/products.json`; `--copy-from` uses one product's current description as the text to write everywhere, so the shop can be brought to a single description without a file.

After each save the script re-reads the page and compares the textarea: a save that did not stick is reported as `NOT saved` and counted as a failure (exit 3), not as success.

`tools/test_update_descriptions.py` covers this against a loopback fake editor — that the form and CSRF token are found, that nothing is posted in a dry run, that a page with no form is read as a missing session, and that multipart forms post too. Python 3 only, no Pillow.
