# The Chợ Tốt public gateway, as measured

Base URL: `https://gateway.chotot.com/v1/public` — no authentication.

Everything here was established by **differential probing** on 2026-08-28: a
parameter counts as working only when changing its value changes the result set
in the direction its name claims. This matters because the gateway's default
failure mode is silent acceptance, not rejection — it answers `HTTP 200` for
parameters it ignores, so a client that trusts parameter names returns
unfiltered data and presents it as filtered.

`chotot/contract.py` holds this as executable data, and `chotot doctor`
re-measures every claim below against the live API.

---

## 1. `GET /ad-listing` — search

### Parameters that work

| Param | Meaning | Evidence |
|---|---|---|
| `q` | free text | 20/20 subject relevance across four sample queries |
| `cg` | category, hierarchical | `cg=2000` returns 2010/2020/2060 |
| `region_v2` | province | 20/20 region match |
| `area_v2` | district | `area_v2=13111` → 10/10 Quận Phú Nhuận |
| `price` | **`MIN-MAX` range string** (`MIN-` for an open top) | `price=0-5000000` → max returned 4,000,000 |
| `limit` | page size, **clamped at 50** | asking 200 returns 50 |
| `o` | offset (**not** `page`) | `o=0` vs `o=200` share 0 ids |
| `account_id` | that seller's ads | `total=28`, 20/20 match |
| `st` | listing type: `s` sale, `u` rent, `k` wanted-buy, `h` wanted-rent | `st=s` → 40/40 sale, `st=u` → 40/40 rent |
| *category facets* | see §5 | 166 verified across 55 categories |

### Parameters accepted and silently ignored

Sending any of these produces an unfiltered result set that looks filtered:

```
sp  ep  minprice  maxprice  price_from  price_to  fromprice  toprice  pf  pt
condition  elt_condition  company_ad  account_oid  seller_id  uid  owner  page
sort (numeric)
```

`sp` is the sharpest trap: it reads like a minimum price and is actually a
listing-type facet — `sp=1` returns ads priced ₫0. Probing `sp=50000000`
returns ₫250,000 listings.

### Parameters that error

`sort=price&direction=asc` → **HTTP 400**. So does bare `direction=asc` when
paired with `sort`. There is **no working server-side sort**; ordering must be
done client-side over a deduplicated pool, and labelled as such.

One name, two meanings: **`direction`** is that sort modifier *and*, on property
categories, a verified facet — "Hướng cửa chính", the main-door direction —
which filters correctly. `cg=1010&direction=1` drops the total from 10,000 to
334. Blanket-refusing the name made the CLI advertise that facet and then refuse
it, so the 400 is recorded against `sort`, where it originates.

### `st` — the filter whose absence is not neutral

Omitting `st` does not mean "everything comparable"; it means "sale and rental
ads in one list". A property browse (`cg=1000`) returns roughly **55% rentals
mixed with 45% sales**, so a median over the unfiltered set averages a monthly
rent against a purchase price.

Measured on apartments (`cg=1010`): the mixed sample reports a median of
**₫6.5 million**; with `st=s` it reports **₫2.52 billion**. Both are arithmetically
correct and only the second is a fact about anything.

A comma list does not union — `st=s,k` returns only `s`. Vehicle and electronics
categories are almost entirely `s`, so this matters most for property.

Set it with `chotot search --listing-type sale` (or `rent`, `wanted-buy`,
`wanted-rent`). Both the search and the analyser warn loudly when a result set
turns out mixed.

### `q` — a union of terms, not an intersection

Every search box teaches users that adding a word narrows the result set. This
one widens it. Measured 2026-08-28 against `region_v2=3017`:

| query | `total` | what came back |
|---|---:|---|
| `canon` | 281 | cameras |
| `v1` | 39 | Honda Winner V1 motorbikes |
| `canon powershot` | 1 | one real Canon PowerShot |
| `canon powershot v1` | **40** | the 39 motorbikes **plus** the 1 camera |

40 = 39 + 1 exactly. Confirmed against two more disjoint pairs, both of which
also summed exactly:

```
powershot=80   vespa=1392   "powershot vespa"=1472   (= 80 + 1392)
nikon=975      vespa=1392   "nikon vespa"=2367       (= 975 + 1392)
```

A token the index does not recognise collapses the whole query instead of being
dropped — `q="zzzqqqxxx v1"` returns nothing while `q="v1"` returns 39 — so this
is a union over recognised terms, not a plain OR over characters.

Ranking makes it worse than a wide result set: the least specific term
dominates, so in the 40-row response the one relevant ad ranked **23rd**. A
`--limit 20` would never have shown it.

**Consequence.** Before this was measured, `analyze "canon powershot v1"
--region "da nang"` reported a median of ₫17,500,000 with no warning. That
figure described motorbikes. The city had zero PowerShot V1 listings; the two
that exist in the whole country are in Bình Dương (₫15,000,000) and Ho Chi Minh
City (₫16,500,000).

There is no server-side way to request an intersection. The client therefore
recomputes it from the ad text the gateway itself returns:

- **unconditionally**, it counts how many returned rows carry every term and
  warns when they do not — a request is not a receipt;
- `--match-all` keeps only the rows that do, and because that filter discards
  rows, the crawl budget counts survivors rather than fetched rows.

`chotot doctor` re-measures this with `powershot` + `vespa` and fails if the
gateway ever starts intersecting.

### Response shape

```json
{"ads": [...], "total": 10000}
```

- **`total` saturates at 10,000.** A broad query and `q=iphone` both report
  exactly 10000, while `q=honda sh` reports a genuine 3095. Above the cap it
  states the cap, not a count — report `≥10000`.
- **`total` is absent, not zero,** when nothing matches. The key is missing
  entirely, so `.get("total")` is `None`. Coercing that to `0` and coercing a
  capped 10000 to a count are the two opposite ways to misreport the same field.
- **`params` is always `[]`** in search results. Condition lives in the
  top-level `elt_condition` integer. Reading it from `params` yields `None` for
  every ad and silently collapses any condition breakdown into one bucket.
- **`date` is relative Vietnamese text** ("2 giờ trước"). The absolute time is
  `list_time`, in epoch **milliseconds**. An export carrying the relative string
  is worthless a week later.

### Pagination

The ceiling is on the **window**, not the offset: `o + limit ≤ 20000` succeeds
and 20001 returns `HTTP 400 "invalid input - reach max search window size"` —
Elasticsearch's `max_result_window`. Verified at two page sizes.

Note this is **not** the `total` cap. Results keep coming well past the point
where `total` saturates at 10,000, so the display cap must never be used as the
crawl bound.

**Adjacent pages overlap.** Measured 8.4% over 5 sequential pages and 7.2% over
8 pages fetched concurrently in 0.75 s. The mechanism is worth stating because
the obvious explanation is wrong: ranking is **deterministic** — the same offset
fetched twice returns byte-identical ids in identical order. The duplication is
structural page-boundary bleed, equally-ranked documents surfacing on more than
one shard page.

The operational consequence: **crawling faster does not reduce duplicates.**
Deduplication by `list_id` is mandatory at any speed, and a crawl must stop when
a window adds nothing new rather than trusting the offset to keep moving.

### Rate limits

No 429, no 503, and **no `RateLimit-*` or `Retry-After` headers exist** up to 8
concurrent requests. The gateway degrades by queueing (190 ms solo → 750 ms at
concurrency 8) rather than rejecting. There is no quota to read, so the client
must self-limit. Escalation beyond concurrency 8 was deliberately not attempted
against a live production service, so the throttle point is **unmeasured, not
absent**.

---

## 2. `GET /ad-listing/{id}` — one listing

Returns `{"ad": {...}, "ad_params": {...}, "parameters": ..., "params": ...}`.

`ad_params` is a dict of `{id, label, value}` carrying Vietnamese labels — this
is where structured specifications live. `ad.phone` is present but **masked**
(`034492****`). A missing or expired id returns **HTTP 404**.

---

## 3. Geography — the 2025 province merger

Two ID scales, and the relationship is not obvious.

- **`region_v2` identifies a province**: `12000` Hà Nội, `13000` HCM, and
  `macro_id * 1000 + area_id` for every other province, where the macro regions
  come from `/chapy-pro/regions`.
- **`area_v2` identifies a district**: `12000+d` / `13000+d` inside the two
  cities, and a seven-digit `{region_v2}{dd}` elsewhere.

Round numbers are **not** valid province codes. Đà Nẵng is `3016`/`3017`, not
`11000`; Cần Thơ is `5027`/`5030`/`5033`, not `14000`. A hand-written table
using the round forms returns zero ads for 11 of 13 provinces — which reads
exactly like "there is nothing for sale there".

**Vietnam merged its provinces in 2025 and Chợ Tốt kept the old codes.** Ads now
display the merged name in `region_name_v3` while `region_name` and the taxonomy
keep the pre-merger name. 74 legacy codes map onto **34 modern provinces**, 23
of which are merger groups:

| Modern province | Legacy codes | Was |
|---|---|---|
| TP Hồ Chí Minh | 2010, 2011, 13000 | Bà Rịa–Vũng Tàu, Bình Dương, HCM |
| TP Đà Nẵng | 3016, 3017 | Quảng Nam, Đà Nẵng |
| TP Cần Thơ | 5027, 5030, 5033 | Cần Thơ, Hậu Giang, Sóc Trăng |
| TP Hải Phòng | 1004, 4019 | Hải Dương, Hải Phòng |
| Lâm Đồng | 7042, 9054, 9057 | Bình Thuận, Đắk Nông, Lâm Đồng |

A repeated `region_v2` parameter returns **HTTP 400**, so covering a merged
province means one request per legacy code, merged and deduplicated.
`chotot search --region hcm` does this; `--no-expand-region` opts out.

**Merging is not concatenation.** The codes sort ascending and the main city
sorts *last* — HCM proper is `13000`, behind Bà Rịa–Vũng Tàu (`2010`) and Bình
Dương (`2011`). Concatenating per-region pages and truncating positionally
therefore returns the annexed provinces and drops the city entirely. Nor is an
equal three-way split right: HCM proper is over 90% of the province's listings
(10,000 capped vs 178 and 893), so an equal sample makes a "Ho Chi Minh City"
median mostly not-the-city. Results are drawn **proportionally to each region's
stated total**.

Two related accounting rules:

- `total` is a property of the query, not of the offset, so every page of one
  region repeats it. Keep **one total per region** and sum across regions;
  appending per page reported 11,071 matches as 22,142.
- Exhaustion is an **AND** across regions. One small region running dry says
  nothing about the province, and reporting the province as exhausted hides an
  under-delivery.

---

## 4. Taxonomy and seller endpoints

| Endpoint | Returns |
|---|---|
| `GET /chapy-pro/categories` | category tree (70 categories) |
| `GET /chapy-pro/regions` | 13 macro regions with their areas |
| `GET /chapy-pro/ad-params?cg=<code>` | per-category parameter definitions with labels and option codes |
| `GET /theia/{account_id or account_oid}` | one seller's complete inventory, with a real `total` |
| `GET /shops/alias/{shop_alias}` | full shop profile, with its first 30 listings embedded |

**Confirmed absent (HTTP 404):** `/profile/{id}`, `/user/{id}`, `/categories`,
`/regions`, `/chotot-rating/...`, `/user-listing`. Seller ratings need no
endpoint — `average_rating_for_seller`, `total_rating_for_seller` and
`is_shop_verified` are already carried on search results.

Two traps on `theia`:

- `limit` is **not** clamped, but `page` and `o` are both accepted and ignored —
  `?o=20` returns the same 20 rows — and `paging.totalPage` advertises pages you
  cannot fetch. The only correct usage is to read `total` from a cheap `limit=1`
  call and then request it in one shot.
- It **echoes the lookup key back** as `account_oid`, so a numeric lookup
  reports the numeric id in that field. Only a 32-character non-numeric value is
  a real OID.
- It describes the listing type with a **different alphabet**: `sell`/`let`
  where `/ad-listing` says `s`/`u`. Left unnormalised, any sale-vs-rent check
  silently never fires on storefront data.
- It carries **no rating or sold-count fields at all**, so seller reputation has
  to come from a separate `ad-listing?account_id=` call. Reporting a rated
  seller as unrated is the failure otherwise.

`/shops/alias/` returns an **unmasked** `phoneNumber` and `additionalPhone1/2`,
while `/ad-listing/{id}` masks the same seller's number. `chotot shop <alias>` redacts
them unless `--show-contact` is passed.

---

## 5. Category facets

`GET /chapy-pro/ad-params?cg=<code>` declares each category's parameters with
their labels and option codes. That is the **posting-form** schema: it says what
a field means, not whether search honours it. So every declared parameter is
additionally probed against `/ad-listing`, and only those whose result set
collapses to the requested value are offered as filters.

Current snapshot: **166 verified working facets across 55 categories**, with 82
declared-but-ignored parameters excluded. Examples:

- **5010 phones** — `mobile_brand`, `mobile_capacity`, `mobile_color`
  (`elt_condition`, `elt_warranty`, `mobile_type` are declared and ignored)
- **2010 cars** — `carbrand`, `carmodel`, `cartype`, `gearbox`, `fuel`,
  `carseats`, `carorigin`, `condition_ad`, `mfdate`
- **5030 laptops** — `pc_brand`, `pc_cpu`, `pc_ram`, `pc_drive_capacity`, `pc_vga`

A facet counts as working only when the probe shows the parameter **changed**
something — the distribution collapsing is not enough on its own. Where a
category's page is already almost entirely one value, "all rows match" is true
whether or not the parameter did anything, which is a check that cannot fail;
`elt_condition` on `cg=5030` passed that way while being ignored. The verdict
now additionally requires the `total` to move or the returned ids to differ.

**Year and size facets are ranges, not scalars.** `mfdate=2019` is accepted and
filters nothing; `mfdate=2019-2020` works — the same `MIN-MAX` shape as `price`.
The CLI refuses a scalar for a range facet rather than forwarding it.

---

## Re-measuring

```bash
chotot doctor                        # 12 graded subjects against the live API
python3 tools/build_taxonomy.py --harvest-areas
python3 tools/build_facets.py
```

`doctor` checks both directions: that supported parameters still filter **and**
that ignored ones are still ignored. A check that can only pass is not evidence,
so if `sp`/`ep` ever start working, doctor reports it as a `WARN` — the contract
would then be stale in the other direction, and those filters could move
server-side.
