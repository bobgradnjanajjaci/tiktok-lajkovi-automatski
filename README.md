# Comment likes dashboard

Private single-operator dashboard. You paste up to ten TikTok video links, it
reads each video's real comment section, finds the first comment that mentions
your author, measures the highest like count in the section, applies your exact
quantity formula, and in Live mode places one God of Panel order per video using
the **video URL + comment owner's username + quantity**.

Setup instructions are in Bosnian below. Code, identifiers and the technical
reference are in English.

---

## What changed in this revision

An independent review installed the declared dependencies, ran the suite and
reproduced several defects. All of them are fixed below.

### Test suite and CI

| Problem | Fix |
|---|---|
| The workflow exported `ADMIN_PASSWORD=ci-password-1234` while `test_app_http.py` signed in with a different literal, and `conftest` used `setdefault`, so the ambient value won. Ten HTTP tests failed with `401 != 303`. | `tests/conftest.py` now **assigns** every setting and exports `TEST_USERNAME` / `TEST_PASSWORD` / `TEST_SESSION_SECRET` as the single source of truth, which the login helper imports. The workflow exports no deployment-shaped variables at all. Production authentication is unchanged and still accepts exactly one password. |
| `test_dockerfile_installs_no_browser` matched the word "Playwright" inside the comment saying there is none. | The check now inspects effective build instructions with comments stripped, also scans `requirements*.txt` for browser packages, and a companion test asserts the comment may still document the decision. |
| `test_no_secret_looking_literals_in_the_source_tree` flagged its own detector definitions. | The markers are split so they cannot match themselves, the module skips itself, and a companion test plants a fake leak to prove the detector still fires. |
| `test_sse_stream_sends_a_snapshot_first` never finished: `TestClient.stream` cannot close an endless SSE body. | New `tests/asgi_driver.py` speaks ASGI directly: it runs the lifespan, reads exactly the frames it needs, sends a real `http.disconnect`, wakes the generator so it observes it, and wraps everything in `asyncio.wait_for`. Both SSE tests are rewritten as bounded async tests. `pytest-timeout` is now a declared dependency and CI runs with `--timeout=60`. |

### Correctness

| Problem | Fix |
|---|---|
| `begin_order_intent` raised `sqlite3.IntegrityError: UNIQUE constraint failed: order_attempts.local_key` when a definitively failed order was followed by a new attempt: the lock was released but the deterministic key collided. | The journal is append-only and the local key carries an attempt sequence (`...#1`, `...#2`). The policy is explicit: a prior `submitting`/`unknown` attempt returns `prior_attempt:unknown`; a prior `accepted` returns `prior_attempt:accepted`; a definitively `failed` attempt allows a new journalled row. A residual collision returns `duplicate_attempt_key` instead of raising. No history is deleted and duplicate protection is not weakened. |
| A record with no `text` field was coerced to `""`, so an unreadable earlier comment looked like a non-matching one and a later match was reported as the first match on a "complete" scan. | `NormalizedComment.text_trusted` distinguishes an explicitly present empty string from a missing, null or non-string value. An untrusted text produces `unreadable_comment_text`, marks the scan incomplete and blocks ordering. The record is kept for diagnostics. |
| One comment carried `user.uid`, another only `user.unique_id`; they were grouped under different keys, so two comments by one account looked like two accounts and same-owner ambiguity was missed. | `target_identity()` joins a record to the target's group when **either** identifier matches, before or after the target, case-insensitively on the handle. A partial identity is never evidence of a different account. A handle shared by two different owner ids is reported as `owner_identity_conflict` plus ambiguity rather than resolved by guessing. A renamed handle under one id is noted. A repeated `cid` still never counts twice. |
| The deadline was checked only after a page had arrived: a 150 ms response under a 10 ms deadline returned after ~151 ms. | Every `__anext__` is bounded by `asyncio.wait_for(remaining)`; on expiry the provider generator is `aclose()`d so no request task outlives the scan. Partial findings are kept, the scan is incomplete, and the worker moves to the next video. The paid submission path is untouched: no blind cancellation or retry was added there. |
| `max_requests=2` allowed four calls (one page plus three owner lookups) and still reported complete, because lookups were counted after the page-level check. | New `RequestBudget` is created per scan, attached to the reader for its duration, and **charged before every outbound attempt** including retries and profile lookups. `requests_made` now comes from that budget, so it cannot drift from what went over the wire. Per-scan statistics stay separate from the adapter's process-wide `call_stats`. |
| `test_opt_in_cannot_excuse_invalid_records` failed with `no_pages_returned:replies:2`: the malformed record omitted `reply_count`, so a reply stream the fixture never supplied was opened. | The fixture sets `reply_count: 0`, isolating the intended condition. Separately, the scanner now accumulates **all** known invalidity reasons instead of hiding later ones behind the first incomplete condition. |

`SCAN_TRUST_PROVIDER_REPLY_END` remains `false` by default. The quantity
function is unchanged.

## The reader: ScrapeCreators

Source verified while writing the adapter (documentation, not a live call):

- Comments — <https://scrapecreators.com/tutorials/how-to-scrape-tiktok-comments-with-python>
- Replies — <https://scrapecreators.com/tutorials/how-to-scrape-tiktok-replies-with-python>
- Replies endpoint spec — <https://scrapecreators.com/tiktok/endpoints/comment-replies>
- Profile parameters — <https://scrapecreators.com/tutorials/how-to-scrape-tiktok-profile-with-python/>
- API docs and sign-up — <https://docs.scrapecreators.com> · <https://app.scrapecreators.com>

### What those pages establish

| Purpose | Call |
|---|---|
| Comments | `GET /v1/tiktok/video/comments` — required `url`, optional `cursor` (number), optional `trim` |
| Replies | `GET /v1/tiktok/video/comment/replies` — required `comment_id` and `url`, optional `cursor` |
| Handle fallback | `GET /v1/tiktok/profile` — `handle` **or** `user_id`, plus `cache_max_age` (a cache hit is documented as 0 credits) |
| Auth | `x-api-key` header |

The published sample responses for both comment endpoints **do** contain `cid`
and `has_more`. Your earlier note came from an OpenAPI example that omitted
them; the current tutorial pages show full sample bodies including them, so the
adapter uses those fields and the tests carry the shapes with provenance marked.

Consumed fields: envelope `success`, `status_code`, `status_msg`, `comments`,
`cursor`, `has_more` (documented as the integer `1`), `total`,
`credits_charged`, `credits_remaining`. Record: `cid`, `text`, `digg_count`,
`reply_comment_total`, `reply_id` (parent `cid`, `"0"` when top level),
`user.uid`, `user.unique_id`.

### What is established, and what is not

- **Established from the documentation:** every field this application needs,
  including the commenter's real handle (`user.unique_id`) directly in the
  comment record. In practice the profile endpoint should be called zero times
  per video.
- **Not established:** ordering stability across requests. ScrapeCreators
  publishes no such guarantee, so the adapter reports its ordering as "provider
  order, stability not documented" and this README does not invent one.
- **Not established:** anything about your account. No live call has been made
  from here. The first real request happens when you run the check command.
- `has_more` appears as the integer `1`. The adapter accepts `1/0`,
  `true/false`, `"1"/"0"`, `"true"/"false"`. Anything else is an **unknown**
  marker and the scan is marked incomplete.
- `cursor` behaves like a numeric offset. It is echoed back verbatim, never
  guessed or incremented locally.
- The provider states there is no account rate limit but advises low
  concurrency. This application reads one video at a time regardless.

### Cost

Reader calls consume ScrapeCreators credits, including read-only ones. **One
video is not one request:** each comment page is a request, and each reply
thread is at least one more. A large video with many threads can cost dozens of
credits. The application does not estimate prices — it reports the provider's
own `credits_charged` and `credits_remaining` from each response, and shows the
running totals in the scan result.

Get a key at <https://app.scrapecreators.com> and put it in `READER_API_KEY`.
That account and its billing are separate from your God of Panel balance.

---

## Uputstvo za postavljanje (Bosanski)

### 1. Raspakivanje i GitHub

1. Raspakuj `comment-likes-dashboard.zip`.
2. Na GitHubu: **New repository**, ime po želji, **Private**, bez README-a i bez
   .gitignore-a (oni su već u paketu). **Create repository**.
3. **Sadržaj** raspakovanog foldera ide u **korijen** repozitorija, ne u
   podfolder. Kad otvoriš repozitorij na GitHubu, `Dockerfile` mora biti odmah
   vidljiv u listi fileova.

```bash
cd <raspakovani-folder>
git init
git add .
git commit -m "Comment likes dashboard"
git branch -M main
git remote add origin https://github.com/<tvoj-korisnik>/<ime-repozitorija>.git
git push -u origin main
```

`.env` se ne šalje na GitHub (već je u `.gitignore`). Nikad ne stavljaj pravi
API ključ u kod, u primjer-fileove ili u commit.

### 2. Povezivanje Railwaya

1. [railway.com](https://railway.com) → prijava GitHub računom.
2. **New Project** → **Deploy from GitHub repo** → odaberi repozitorij.
3. Railway sam prepozna `Dockerfile` u korijenu i pokrene build.

### 3. Varijable (Railway → Variables)

Otvori servis → **Variables** → **New Variable**. Ovo su vrijednosti koje **ti**
unosiš:

| Varijabla | Vrijednost |
|---|---|
| `API_KEY` | `<tvoj God of Panel ključ>` |
| `PANEL_URL` | `https://godofpanel.com/api/v2` |
| `SERVICE_ID` | `5836` |
| `COMMENT_READER` | `scrapecreators` |
| `READER_BASE_URL` | `https://api.scrapecreators.com` |
| `READER_API_KEY` | `<tvoj ScrapeCreators ključ>` |
| `READER_API_KEY_HEADER` | `x-api-key` |
| `KEYWORD` | `Mael Vorran` |
| `COMMENT_SCOPE` | `all` |
| `SCAN_TRUST_PROVIDER_REPLY_END` | `false` |
| `RUN_MODE` | `dry_run` |
| `ADMIN_USERNAME` | `<tvoje korisničko ime>` |
| `ADMIN_PASSWORD` | `<jaka lozinka, 12+ znakova>` |
| `SESSION_SECRET` | `<nasumično, 32+ znakova>` |
| `DATABASE_PATH` | `/data/app.db` |
| `ENVIRONMENT` | `production` |
| `COOKIE_SECURE` | `true` |

Nasumični `SESSION_SECRET`:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

`READER_CONTRACT_FILE` **nije potreban** za `scrapecreators`. Puna tabela svih
varijabli s defaultima je niže.

### 4. Volume (obavezno)

Bez volumena se baza gubi pri svakom redeployu, a s njom historija narudžbi i
zaštita od duplih narudžbi. Folder napravljen u Dockerfileu **nije** trajna
memorija.

1. Servis → **Settings** → **Add Volume**.
2. Mount path: `/data`.
3. Sačuvaj, pusti redeploy, i provjeri da `DATABASE_PATH` ostaje `/data/app.db`.

### 5. Jedna replika

Servis → **Settings** → **Deploy** → **Replicas** = `1`. Arhitektura koristi
jedan Uvicorn proces i jedan worker nad jednim SQLite fileom.

### 6. Isključivanje Serverless / App Sleeping

Servis → **Settings** → **Serverless** (odnosno **App Sleeping**) → isključi, pa
redeploy ako Railway to zatraži. Uspavan servis dodaje kašnjenje na prvi
zahtjev. Servis koji stalno radi troši resurse cijelo vrijeme; to je očekivano.
Nema self-ping petlje i namjerno je nema.

### 7. Javna adresa

Servis → **Settings** → **Networking** → **Generate Domain**. Railway koristi
`PORT` iz okruženja i provjerava `/health`, što aplikacija podržava.

### 8. Logovi

Servis → **Deployments** → zadnji deploy → **View Logs**. Pri normalnom
pokretanju vidiš `starting on port ...` i zatim
`started: reader=scrapecreators configured=True panel_configured=True db=/data/app.db`.

### 9. Prvi Dry run, pa tek onda Live

1. Otvori adresu i prijavi se.
2. U zaglavlju provjeri: `Reader scrapecreators · reading the live API`. Ako
   piše `not configured`, `READER_API_KEY` nije postavljen.
3. **Check service & balance** — mora naći servis 5836, potvrditi kompatibilan
   tip i prikazati stanje.
4. Zalijepi **jedan stvarni** video link, ostavi **Dry run**, klikni **Start**.
   Pogledaj: izabrani komentar, pravi handle, `top likes`, `quantity`, scan
   status.
5. Tek kad Dry run ima smisla, odaberi **Live** i klikni **Start**. Izbor Live
   moda i pritisak na Start odobravaju narudžbe za taj batch; nema dodatnog
   pitanja po linku.
6. `submitted` znači da je panel primio narudžbu, ne da su lajkovi isporučeni.
   Dostava se prati odvojeno.

### 10. Lokalno (opcionalno)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env      # postavi ENVIRONMENT=development, DATABASE_PATH=./data/app.db,
                          # COOKIE_SECURE=false, COMMENT_READER=fixture
python -m pytest -q
./start.sh
```

Sa `COMMENT_READER=fixture` rade samo tri demo videa iz
`tests/fixtures/comments` (`7300000000000000001/2/3`). Zaglavlje tada jasno piše
**demo data — not TikTok** i Live je onemogućen.

### 11. Provjera stvarnog čitanja (jedna komanda)

Kad imaš ključ:

```bash
export READER_API_KEY=<tvoj-scrapecreators-kljuc>
export COMMENT_READER=scrapecreators
python scripts/check_integrations.py "https://www.tiktok.com/@neko/video/7300000000000000001"

# uz read-only provjeru panela:
export API_KEY=<tvoj-god-of-panel-kljuc>
python scripts/check_integrations.py --panel --max-pages 3 "<link>"
```

Skripta ispisuje razrješenje URL-a, paginaciju, normalizaciju, odgovore, pravi
handle, maksimum, potpunost skena i Dry run količinu. **Ne postoji kodni put do
`action=add`, `refill` ili `cancel`.** Ključevi se ne ispisuju. `--max-pages`
ograničava potrošnju kredita pri prvoj provjeri.

---

## Quantity rules

The function in `app/like_rules.py` is yours, unchanged. `quantity` is the
number of **additional** likes ordered, not a desired final total.

| `top_likes` | quantity |
|---:|---:|
| 0 – 100 | 150 |
| 101 – 300 | 500 |
| 301 – 999 | `int(top_likes * 1.3)` |
| 1000 – 2999 | `top_likes * 2` |
| 3000 – 7999 | `top_likes + 1000` |
| 8000 – 9999 | `top_likes` |
| 10000+ | 0, no order |

Verified boundaries: 0→150, 99→150, 100→150, 101→500, 249→500, 280→500, 299→500,
300→500, 301→391, 999→1298, 1000→2000, 2999→5998, 3000→4000, 7999→8999,
8000→8000, 9999→9999, 10000→0, 15000→0.

**The step down at 300 → 301 is intentional.** 300 orders 500, 301 orders 391.
It is not smoothed.

**The formula is not a ranking strategy.** It does not subtract the target's
current likes, add a margin, or skip a target that is already leading. A target
at 0 with a leader at 8,000 receives 8,000 additional likes and would only
*tie*, and only if every one is delivered.

Invalid like counts are rejected rather than coerced: negatives, booleans,
`None`, floats, `"1,200"`, `"12.0"`, `"1.2K"` and empty strings all fail.
**Missing data is not zero.** Zero counts only when the source reports zero.

---

## How one video is processed

1. `url_resolver` resolves the pasted link. Canonical links need no request.
   `vm.tiktok.com`, `vt.tiktok.com` and `www.tiktok.com/t/` are followed with a
   bounded redirect chain; every hop is re-validated against the TikTok host
   allowlist and refused if it resolves to a private, loopback, link-local or
   reserved address. All aliases of one video resolve to the same id.
2. `comment_finder.scan_video` traverses the scope **once**, keeping three
   things at the same time: the first keyword match in source order, the running
   maximum like count over **all** comments in scope, and every comment grouped
   by its owner. Nothing is sorted and nothing is fetched twice.
3. Traversal for scope `all`: parent in source order, then that parent's replies
   in source order, then the next parent.
4. One early exit: any trustworthy comment with 10,000+ likes forces quantity 0,
   so the scan stops with `skipped_threshold` and is explicitly **not** marked
   complete.
5. `calculate_quantity` runs on the observed maximum.
6. Live only: target verified → service metadata valid → quantity within live
   limits → no prior/active order → take the per-video lock → journal a
   `submitting` intent → one POST. Dry run ends at `dry_run_complete` with zero
   panel order calls.
7. The result is persisted and the worker moves on immediately. Delivery is
   tracked in a separate bounded loop.

### Completeness is never fudged

`complete` requires every requested stream to report its end. All of these
produce `scan_incomplete` and block ordering:

- a page arrives with `has_more` still set and the stream stops;
- `has_more` is set but no cursor was returned;
- the same cursor is returned twice;
- the has-more marker is absent or in an unrecognised shape;
- the stream yields **no pages at all**;
- a parent reports N replies and fewer distinct replies were collected;
- any budget is hit (`SCAN_DEADLINE_SECONDS`, `SCAN_MAX_PAGES`,
  `SCAN_MAX_REQUESTS`, `SCAN_MAX_COMMENTS`, `SCAN_MAX_THREADS`);
- any like count in scope failed validation;
- a comment's text was missing, null or not a string, so "the first match"
  cannot be proven (`unreadable_comment_text`);
- some comment carried no owner identifier, so target uniqueness is unprovable;
- one handle is used by two different owner ids (`owner_identity_conflict`);
- the overall deadline expired, including while awaiting a provider response;
- the per-scan request budget was reached before a stream could finish;
- the provider cannot read replies while scope is `all`.

Hitting a limit means incomplete, never "complete enough". The observed maximum
is still shown, always labelled as the maximum **seen**.

### The reply-count mismatch, and the opt-in

A parent advertises `reply_comment_total`, but the replies endpoint may return
fewer distinct replies. That gap can mean hidden or removed replies, nested
replies the endpoint does not expose, visibility differences between what the
provider sees and what you see, or a counter that changed during the scan.

**It does not prove that more replies are retrievable. It equally does not prove
that the maximum across the scope you asked for is known.** So the shipped
default `SCAN_TRUST_PROVIDER_REPLY_END=false` treats any shortfall as
`scan_incomplete` and places no order, even when the replies endpoint says it
has ended.

`SCAN_TRUST_PROVIDER_REPLY_END=true` is an explicit opt-in. It changes what
"complete" means: the result then covers **provider-visible data only**, not the
whole comment section. When it is on:

- every affected result carries a `scope_limitations` entry naming the comment
  and the exact shortfall (`3 of 9 advertised replies were returned`), shown in
  the dashboard next to the scan status — not only in the logs;
- `completeness_basis` in the stored scan JSON reads
  `provider-visible data (reply-count mismatches accepted)`;
- the dashboard header shows a **reply-count mismatches accepted** tag;
- it still cannot excuse a stream that reports `has_more`, an unknown completion
  marker, invalid records, or an exceeded budget.

It is never enabled silently, and no result is ever described as every TikTok
comment.

---

## Target mapping

```python
payload = {
    "key": settings.API_KEY,
    "action": "add",
    "service": settings.SERVICE_ID,   # 5836
    "link": target.video_url,         # the VIDEO URL
    "quantity": quantity,             # additional likes
    "username": target.comment_owner_username,
}
```

URL-encoded form data via `client.post(settings.PANEL_URL, data=payload)`. TLS
verification stays on. Redirects are **not** followed, so the key cannot be
replayed to another host. No comment permalink, no comment id, no invented
field. The God of Panel key is never sent to ScrapeCreators, and the reader key
is never sent to the panel.

`username` is the account handle only. One presentation-only leading `@` is
stripped; the original is kept in diagnostics. A display name is never
substituted and a handle is never guessed from the comment text.

### The real precision limit

The payload carries video + username, not a comment id. If the selected owner
wrote **more than one** comment under that video — with or without the keyword,
before or after the target — the payload cannot express which one. The scan
detects that by grouping every comment on the stable owner id (`user.uid`,
falling back to the handle only when that is the sole identifier) and returns
`target_ambiguous`. The same `cid` returned twice is not a second comment. No
invented comment-id parameter is sent to bypass this, and a different matching
comment is never silently substituted. Other videos in the batch are unaffected.

### Runtime service validation

Before Live submission the app reads `action=services`, finds 5836, and
validates name/type and the live min/max/rate; metadata is cached for
`SERVICE_METADATA_TTL_SECONDS`. `action=balance` supplies balance and currency,
handled as `Decimal`. If 5836 is missing or would not accept `username`, the
item becomes `service_configuration_required`. The quantity is validated against
the live limits and **never** clamped, rounded or split — out of range means
`quantity_out_of_range`. An order of zero is never sent. The screenshot values
(0–5 minutes, min 10, max 1,000,000) were what was displayed then, not
guarantees.

---

## Duplicate spending protection

- **Idempotency token** on submission: a double click or browser retry returns
  the existing batch.
- **Per-video lock** keyed by panel URL + canonical video id. Another alias,
  another username or another batch cannot bypass it.
- **Intent before request:** a `submitting` row is committed inside the same
  transaction that takes the lock, before the HTTP call.
- **The attempt journal is append-only.** Each attempt gets its own sequenced
  key, so a repeat after a documented rejection is a new row rather than a
  database error, and nothing in the history is overwritten. A repeat is
  allowed only when the previous attempt was a definitive rejection; a prior
  `unknown` or `accepted` attempt returns a controlled reason and sends
  nothing.
- **`action=add` is never retried automatically.** Timeout, connection loss,
  5xx, malformed success body or a crash leaves `submission_unknown` with the
  lock held. A documented definitive rejection is distinguished from an
  uncertain outcome and releases the lock.
- **Accepted orders stay locked until `status` reports completion.** An elapsed
  estimate releases nothing. Partial/cancelled/error become `needs_review`; no
  automatic top-up ever happens.
- **Restart recovery:** `submitting` becomes `unknown` and is not requeued;
  claimed jobs that never reached an attempt are requeued because reading is
  idempotent; accepted orders resume status tracking by their existing order id.
- **Dry runs reserve nothing** and leave no lock. A Live run performs a fresh
  scan; it never converts an old Dry run result into a purchase.
- One failed link does not stop the batch. Stop prevents new work; an accepted
  remote order does not disappear.

**Limitation:** the documented API exposes `add`, `services`, `balance` and
`status` for a known order id, with no order-history endpoint. If you place an
order manually in the panel's own interface, this app cannot see it and cannot
warn you.

---

## Outcomes

Processing state and delivery state are separate. `delivered` is never shown
merely because an order id exists.

| Outcome | Meaning |
|---|---|
| `pending` / `processing` | Queued, or being worked on |
| `keyword_not_found` | Scope completed, no comment contained the keyword |
| `scan_incomplete` | Completeness could not be established; no order |
| `skipped_threshold` | A comment already has 10,000+ likes, so quantity is 0 |
| `target_ambiguous` | That account wrote several comments under this video |
| `target_unverified` | No authoritative handle, or unverified canonical URL |
| `quantity_out_of_range` | Outside the live service min/max; not clamped |
| `service_configuration_required` | Service 5836 missing or incompatible at runtime |
| `reader_unconfigured` | `READER_API_KEY` missing, or the fixture reader is selected |
| `url_invalid` | Not a usable TikTok video link |
| `provider_error` | The reader or link resolution failed |
| `already_ordered` | A completed order already exists for this video |
| `active_order_exists` | An order for this video is in flight or unresolved |
| `dry_run_complete` | Analysed, nothing bought |
| `submitted` | The panel accepted an order and returned an order id |
| `submission_unknown` | Outcome genuinely unknown; resolve manually |
| `failed` | The panel rejected the order; nothing was created |
| `cancelled` | Stopped before submission |

Delivery states come only from `action=status`: `unknown`, `pending`,
`in_progress`, `processing`, `partial`, `completed`, `canceled`, `error`.

---

## Full configuration reference

Defaults below are the values in `app/config.py` and are enforced by a test.

| Variable | Default | Required | Notes |
|---|---|---|---|
| `API_KEY` | *(empty)* | for Live | God of Panel key |
| `PANEL_URL` | `https://godofpanel.com/api/v2` | no | https only |
| `SERVICE_ID` | `5836` | no | kept as a string |
| `SERVICE_METADATA_TTL_SECONDS` | `900` | no | service metadata cache |
| `KEYWORD` | `Mael Vorran` | no | |
| `COMMENT_SCOPE` | `all` | no | `all` or `top_level` |
| `RUN_MODE` | `dry_run` | no | dashboard always starts on Dry run |
| `COMMENT_READER` | `scrapecreators` | no | `scrapecreators`, `http`, `fixture` |
| `READER_BASE_URL` | `https://api.scrapecreators.com` | no | |
| `READER_API_KEY` | *(empty)* | **yes** | reader key |
| `READER_API_KEY_HEADER` | `x-api-key` | no | |
| `READER_CONTRACT_FILE` | *(empty)* | only for `http` | not used by `scrapecreators` |
| `READER_FIXTURE_DIR` | `tests/fixtures/comments` | no | demo only |
| `READER_PAGE_SIZE` | `50` | no | ignored by `scrapecreators` |
| `READER_TIMEOUT_SECONDS` | `15` | no | |
| `READER_MAX_CONNECTIONS` | `10` | no | |
| `READER_TRIM` | `false` | no | keep false for full responses |
| `READER_OWNER_CACHE_TTL_SECONDS` | `3600` | no | handle cache |
| `READER_PROFILE_CACHE_MAX_AGE` | `7d` | no | provider cache window |
| `SCAN_DEADLINE_SECONDS` | `90` | no | covers network, retries, replies |
| `SCAN_MAX_PAGES` | `60` | no | |
| `SCAN_MAX_REQUESTS` | `120` | no | |
| `SCAN_MAX_COMMENTS` | `5000` | no | |
| `SCAN_MAX_THREADS` | `200` | no | reply threads expanded |
| `SCAN_TRUST_PROVIDER_REPLY_END` | `false` | no | strict; see above before changing |
| `ADMIN_USERNAME` | `admin` | no | |
| `ADMIN_PASSWORD` | *(empty)* | **yes** | 12+ chars in production |
| `SESSION_SECRET` | *(empty)* | **yes** | 32+ chars in production |
| `SESSION_TTL_SECONDS` | `86400` | no | |
| `COOKIE_SECURE` | `true` | no | false only for local http |
| `DATABASE_PATH` | `./data/app.db` | **yes on Railway** | use `/data/app.db` |
| `STATUS_POLL_INTERVAL_SECONDS` | `60` | no | delivery polling |
| `STATUS_POLL_BATCH_SIZE` | `20` | no | |
| `ENVIRONMENT` | `production` | no | production enforces real secrets |
| `MAX_LINKS_PER_BATCH` | `10` | no | |

---

## Latency

- One pooled `httpx.AsyncClient` per host with keep-alive and bounded limits.
- `POST /api/batches` returns as soon as rows are committed; processing runs in
  a lifespan-owned worker, so closing the dashboard does not stop a batch and a
  restart resumes from SQLite without a browser action. No worker is spawned per
  HTTP request.
- The worker sleeps on an `asyncio.Event` and is woken the moment a batch is
  enqueued, then drains every ready job back to back. No fixed polling interval
  between ready videos, no artificial pause, no `time.sleep`, no page reloads,
  no media downloads.
- Videos are processed strictly sequentially, one active analysis/submission.
- Dependent requests are never issued concurrently and cursors are never
  guessed. There is no second scan for the maximum.
- The handle comes from the comment record; a lookup happens only when it is
  missing, once per distinct owner id, cached. Commenter profile pages are never
  opened.
- `Retry-After` and bounded backoff are honoured on safe reads only (3 attempts,
  capped). Retry time is recorded separately from useful work. The deadline
  covers network waiting, retries, replies and lookups.
- Delivery polling is a separate bounded loop that cannot stall the worker.
- Progress streams over authenticated SSE with a reconnect snapshot.
- Per-item timings: URL resolution, comment read, owner lookup, calculation,
  submission, total — plus page and request counts.

**No promise of zero delay.** Network latency, pagination volume, provider
processing and SMM delivery are external. Processing time and delivery time are
never merged.

---

## Security

- Dashboard, history, API and SSE all require a session.
- Signed HttpOnly SameSite=Lax cookies, `Secure` by default. Production refuses
  to start without a real `ADMIN_PASSWORD` (12+) and `SESSION_SECRET` (32+).
- CSRF token on every mutation.
- Comment text is rendered with `textContent` only, including SSE updates.
  Jinja autoescaping is on; no template uses `|safe`.
- Only HTTPS TikTok hosts are accepted; every redirect hop is re-validated;
  private and local addresses are refused.
- API keys are never logged, never rendered and never sent to the browser.
  Outbound payloads are sanitized before logging.
- `/health` is public, secret-free and contacts **no external service**: it
  touches the database and reports adapter names and boolean flags only. It
  cannot consume reader credits or create orders, however often it is probed.

---

## Project layout

```
app/                                   FastAPI app, worker, scanner, adapters
app/providers/scrapecreators_reader.py the live reader
app/providers/contract_http_reader.py  optional generic adapter
app/providers/fixture_reader.py        demo/test data only
scripts/check_integrations.py          opt-in real-API check (never orders)
scripts/offline_checks.py              dependency-free verification runner
tests/asgi_driver.py                   bounded ASGI driver for the SSE tests
tests/                                 pytest suite, all mocked
.github/workflows/tests.yml            CI: pytest + Docker build/health/volume
Dockerfile, start.sh, railway.json     deployment, all at the repo root
.env.example, .gitignore, .dockerignore
README.md
```

No root `app.py`, no `Procfile`. The single deployment path is Docker +
`start.sh`, which `exec`s Uvicorn so SIGTERM reaches the server.

---

## Verification status

Three distinct claims, kept apart.

### Executed here, and passing

`pip install` is impossible in the container this package was assembled in:

```
$ pip install -r requirements.txt -r requirements-dev.txt
ERROR: Could not find a version that satisfies the requirement fastapi==0.115.6 (from versions: none)
ERROR: No matching distribution found for fastapi==0.115.6
```

So pytest could not be used. Everything that runs on the standard library alone
was executed instead, including the test modules themselves, through a minimal
pytest stand-in that supplies the `tmp_path`, `db` and `fixture_dir` fixtures
and expands `parametrize`:

| What ran | Result |
|---|---|
| `python scripts/offline_checks.py` | **174 passed, 0 failed** |
| `tests/test_like_rules.py` | **40 passed, 0 failed** |
| `tests/test_keyword_matching.py` | **31 passed, 0 failed** |
| `tests/test_scanner.py` | **40 passed, 0 failed** |
| `tests/test_scan_budget.py` | **9 passed, 0 failed** |
| `tests/test_database.py` | **15 passed, 0 failed** |
| Deployment-file assertions, executed by hand | **16 passed, 0 failed** |
| `python -m py_compile` over every `.py` | passed |
| `node --check app/static/app.js` | passed |

That covers, specifically: every quantity boundary and the 300→301 step down;
keyword matching; the scanner's traversal, completeness, text-trust and identity
rules; both reply-count settings and the three things the opt-in cannot excuse;
the ScrapeCreators adapter's parsing, pagination, error handling and credit
accounting against a scripted transport; the request budget and the deadline,
asserting the number of calls the transport actually received; and the order
journal including the `UNIQUE constraint` reproduction and the repeat-attempt
policy.

### Written and mock-tested, but NOT executed here

These need fastapi or a real httpx and therefore did not run:
`tests/test_app_http.py` (including both rewritten SSE tests),
`tests/test_worker_e2e.py`, `tests/test_smm_client.py`,
`tests/test_url_resolver.py`, `tests/test_reader_contract.py`,
`tests/test_scrapecreators_reader.py`, `tests/test_deployment_files.py`. They
byte-compile and every transport in them is a mock. **I am not claiming they
pass.** Run them where you have network:

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

The GitHub Actions workflow runs exactly that plus the offline checks, and its
credentials come only from `tests/conftest.py`. **It has not run**; its first
run is on your first push.

### Not verified at all

- **No live ScrapeCreators call.** No key exists here. The first real response
  is the one your `check_integrations.py` run produces. If it shows a schema or
  pagination difference, send me the redacted output and I will fix the adapter.
- **No live God of Panel call.** No order has been created.
- **No Docker build or Railway deploy.** Docker is unavailable in this
  container. The CI `docker` job builds the image, starts it on a non-default
  `PORT`, probes `/health`, checks the response carries no secret, and restarts
  it against the same volume — but only when you run it.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Railway build fails, "no build configuration" | `Dockerfile` is not at the repository root. Move the files up one level and push again. |
| Deploy crashes on start | Missing `ADMIN_PASSWORD` or `SESSION_SECRET` in production fails fast by design. Set them in Variables. |
| `FATAL: /data is not writable` | No volume, or not mounted at `/data`. Add it, keep `DATABASE_PATH=/data/app.db`, redeploy. |
| Healthcheck fails | The app must bind Railway's `PORT`; it does via `start.sh`. If you overrode the start command, restore `/app/start.sh`. |
| History empty after every redeploy | Running without a volume. A Dockerfile `mkdir` is not persistent storage. |
| Header says "not configured" | `READER_API_KEY` is missing. |
| Header says "demo data — not TikTok" | `COMMENT_READER=fixture`. Set it to `scrapecreators`. |
| Every item is `reader_unconfigured` | Same two causes as above. |
| `provider_error ... HTTP 401` | Wrong or expired `READER_API_KEY`. |
| `provider_error ... no credits left (HTTP 402)` | Top up the ScrapeCreators account. Nothing was ordered. |
| `provider_error ... HTTP 404` | The video is private, removed or mistyped. Not an empty comment section. |
| Every item is `scan_incomplete` | Open Details and read `incomplete_reasons`. Usually a reply-count shortfall (strict default), a stalled cursor, or a tight budget. |
| `target_ambiguous` | That account wrote more than one comment under the video. Order manually or pick another video. |
| `service_configuration_required` | `action=services` did not return 5836 or its type would not accept `username`. Use **Check service & balance**. |
| `submission_unknown` | Check the panel's own order list before anything else. The app will not retry and keeps that video locked. |
| `already_ordered` right after a Live run | A prior attempt for this video and owner is `accepted`. Resolve its delivery first; nothing was sent. |
| `scan_incomplete ... unreadable_comment_text` | The provider returned a comment with missing, null or non-string text, so the first match cannot be proven. |
| `scan_incomplete ... owner_identity_conflict` | Two different owner ids share the target handle in this scope. Ordering by username would be a guess. |
| `scan_incomplete ... max_requests_reached` | The scan needed more calls than `SCAN_MAX_REQUESTS` allows. Raise it, or narrow the scope. Reader credits are charged per call. |
| `scan_incomplete ... scan_deadline_reached` | `SCAN_DEADLINE_SECONDS` expired, possibly mid-response. Raise it for very large videos. |
| First request after idle is slow | Serverless / App Sleeping is still on. |
| `403 CSRF token missing or invalid` | The page was open across a restart with a rotated `SESSION_SECRET`. Sign in again. |

---

## Honest limitations

1. **No live verification.** Reader and panel integrations are implemented and
   mock-tested. Neither has contacted a real service from here.
2. **Provider visibility is not TikTok's ground truth.** An exhausted traversal
   is what one provider could see in one window. Source order is not a promise
   about the order shown in every person's app, and ScrapeCreators publishes no
   ordering-stability guarantee.
3. **No order-history detection.** Orders placed manually in the panel are
   invisible to this app.
4. **An order id is acceptance, not delivery.** Ranking is never guaranteed.
5. **Reader credits are consumed by reading**, including by the check script.
   One video is not one request.
6. **No uninterrupted availability.** Redeploys interrupt the service briefly
   even with a volume attached.
7. **Buying engagement is against TikTok's terms of service.** The accounts
   involved and the orders themselves can be actioned, and delivery is never
   guaranteed regardless of what the panel reports. Worth weighing before Live.
