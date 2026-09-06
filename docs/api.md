# The Pignus HTTP API

Two processes speak HTTP here. `pignusd` is the loan book: it serves the page,
lists offers and loans, follows them on the chain, and carries messages between
the two parties of a cross-chain loan. `pignus-oracle` signs prices and
publishes every attestation it has ever made.

Neither holds a key or a coin. Everything either of them says about a loan is
derived from the terms and from the chain, and a borrower checks the vault
address themselves before signing anything — so this API is an index and a
message board, not an authority. Read it that way: an offer served here is worth
checking against the chain, and an attestation served here is worth verifying
against the key the vault bakes in.

## Conventions

**Base URL.** `pignusd` listens on `127.0.0.1:8741` by default and is published
under a path prefix by the reverse proxy; on the testnet that is
`https://sequentiatestnet.com/lending/`, and the page fetches everything by the
relative path `v1/...`. The oracle listens on `127.0.0.1:8730` by default (8740
on the testnet box) and is published under `/pignus-oracle/`. Every further
oracle a book quotes gets a route of its own — `/pignus-oracle-2/` and
`/pignus-oracle-3/` on the testnet — because an m-of-n seizure is signed by
oracles that are not the primary, and its attestation is published only by the
oracle that signed it. `/v1/oracles` lists every key with its public address.

**Content type.** Requests and responses are JSON. A POST body that is not an
object, or is not valid JSON, is a 400.

**Numbers.** Amounts in asset atoms are **decimal strings**, because an atom
count can exceed what a JSON number holds exactly in a browser. That is true of
the `terms` document this book serves as well as of the fields around it:
`collateral_amount`, `principal`, `debt`, `strike`, `max_price` and
`not_before` are strings inside it, and a cross-chain offer's `btc_amount`,
`debt`, `principal` and `strike` are strings for the same reason.
`web/pignus.js` refuses a number above 2^53
outright rather than compute a debt `JSON.parse` may already have rounded, so a
book that served them as numbers would hold loans its own page could not
price.

Every one of those amounts is coerced to a decimal string when a record is
written and again when it is served, so the shape holds for every record
whatever wrote it. The decimal-string rule holds for the amounts a book
derives as well as the ones it stores. Heights,
locktimes, timestamps, confirmations, counts, prices, `price_scale` and
`expiry_locktime` are JSON numbers. That holds for the amounts a book derives
as well as the ones it stores: `seizure_if_liquidated` and
`surplus_if_liquidated` on a loan, every amount inside `/v1/loans/{id}/exit`,
and the totals in `live_debt_by_asset` are all decimal strings too. Those are
the figures a lender checks a payout against, and a rounded one reads as a
shortfall that is not there.

The `terms` field of an offer or a loan is the JSON **string** that was
submitted, stored and served back byte for byte -- not a nested object. That is
deliberate: the terms are what every party's covenant address is derived from,
so re-serialising them here would be a chance to change them. Parse it
yourself. Its amount fields are decimal strings whenever this book, the CLI or
the page writes them; a book accepts either spelling on the way in, because a
browser cannot serialise an integer above 2^53 exactly.

**Prices.** A price is debt-asset atoms per collateral-asset atom, multiplied by
`price_scale`. `unit_price` in `/v1/markets` is that number divided out, as a
float, for display only — never compute against it.

**Errors.** Every failure that carries a body carries `{"error": "a sentence"}`
with the status code below. The sentence is written to be shown to a person, so
it says what was wrong rather than naming a Python class. A `DELETE` on a
listing that is not there answers 404 with `{"removed": false}`.

**Limits.** POST and DELETE are charged to a client at one request a
second with a burst of twenty, and to everybody together at twenty a second
with a burst of two hundred. Reads that make the node work -- `/v1/spend`,
`/v1/outpoint`, `/v1/scan` and `/v1/btc/outpoint` -- are charged at two a
second with a burst of thirty per client, and forty a second with a burst of
four hundred together. The whole-book listings -- `/v1/loans`, `/v1/offers`,
`/v1/btc/offers` and `/v1/stats` -- are charged at two a second with a burst
of thirty per client; the page asks for each once every thirty seconds. Over
any of those the answer is 429. Every other read is served from memory and is
not limited, except `/v1/loans/{id}/exit`, which reads the closing block from
the node once and answers from a cache after that.

**Listings.** The four listings are rendered once and served from a cache
until the book changes, the poll learns something new, or ten seconds pass,
whichever is first; every tab asking within that window gets the same
render. They are sent compact, and gzipped when the request carries
`Accept-Encoding: gzip` (a full book compresses about tenfold). When more
than eight renders are already running the answer is a 503 with
`Retry-After: 5` rather than a queue without bound. The server holds at most
256 connections at once; past that an accept is closed unread and a client
that retries gets a slot when one frees.

Every write -- each `POST`, and `DELETE` -- must come from this site. A `POST`
must carry `Content-Type: application/json`, and an `Origin` header naming
another site is refused, both with a 400 before the body is parsed. Nothing a
write can do moves money without a coin or a signature; what this refuses is
a page elsewhere spending a visitor's own rate-limit bucket from their
browser. Reads stay open to every origin. Every answer carries a content
security policy that keeps the page's scripts and connections to this site
and forbids framing it, plus `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.

**Behind a proxy.** Behind a reverse proxy every request arrives from loopback, so `pignusd`
believes `X-Forwarded-For` (or `X-Real-IP`) only from a peer listed in
`trusted_proxies`, and only its last hop — earlier entries are whatever the
client claimed. A request from a trusted peer *without* that header is the box's
own tooling and is not rate limited at all. So `pignusd` must never be reachable
except through the proxy: exposed directly, every public client would arrive
unheadered and unlimited.

**CORS.** Reads carry `Access-Control-Allow-Origin: *`; writes carry no CORS
header at all, and the preflight advertises `GET` only. Reading the book from
elsewhere is welcome; writing to it from a visitor's browser on someone else's
page is not.

**Caching.** Every response carries `Cache-Control: no-store`. The whole point
of the book is that it is current.

**Body size.** A request body over 256 KiB is refused with 400.

**The page's own files.** `GET /` serves `web/index.html`. A bare filename
ending `.js`, `.json`, `.css` or `.svg` is served from `web/` if it exists
there, and nothing else is: no directories, no traversal, no listing.

---

# `pignusd`

## Reference data

### `GET /v1/markets`

Every market this book is configured for, whether it can be lent in, and the
latest verified price.

```json
{
  "markets": [
    {
      "market": "GOLD/USDX",
      "feed_id": "…64 hex…",
      "collateral_ticker": "GOLD", "debt_ticker": "USDX",
      "collateral_asset": "…64 hex…", "debt_asset": "…64 hex…",
      "collateral_precision": 8, "debt_precision": 8,
      "oracle_precisions": [8, 8], "precision_mismatch": false,
      "cross_chain": false,
      "lendable": true,
      "stale": false,
      "debt_is_reference": true,
      "price": 300000000, "price_scale": 100000,
      "unit_price": 3000.0,
      "timestamp": 1799999940, "age_seconds": 21,
      "oracles_available": 3
    }
  ],
  "height": 118432,
  "btc_height": 155377,
  "btc_feerate_sat_vb": 12.5,
  "explorer_tx_url": "https://…/tx/{txid}",
  "btc_explorer_tx_url": "https://…/tx/{txid}",
  "reference_ticker": "USDX",
  "block_seconds": 60,
  "min_depth": 2,
  "max_price_age": 600
}
```

`btc_feerate_sat_vb` is what the parent chain is charging now, and it is
**null** rather than a guess when this book has no Bitcoin node or the node will
not estimate. A cross-chain loan's move into the vault is signed at origination
and can be neither replaced nor paid for by a child, so a borrower has to judge
the fee that offer committed against this number before they commit anything --
and against nothing at all rather than against an invented rate.

`explorer_tx_url` and `btc_explorer_tx_url` are templates with a `{txid}`
placeholder, or empty strings when the operator configured none.

`lendable` is false when either ticker is unknown to the registry, when the
price is not current, or when the precisions disagree; `stale` is true when a
price is held and is not current. Current means BOTH young enough -- no older
than `max_price_age` -- and not dated more than a two-minute clock skew ahead
of the book's own clock. The second half matters as much as the first: an
oracle host running fast signs a real price, its feed then dies, and for as
long as the drift lasts a one-sided test reads that dead number as infinitely
fresh. `cross_chain` marks a market
whose collateral is native Bitcoin: those are never lendable through the
covenant offers and are traded through `/v1/btc/*` instead.
`oracles_available` counts the oracles that have a currently verified
attestation for the market, which is what a lender needs before opening an
m-of-n loan.

`oracle_precisions` is the `[collateral, debt]` decimal count the primary oracle
says it scaled the price by; `precision_mismatch` is true when that disagrees
with the registry. They are the same two numbers arrived at independently, and
when they differ the price is out by a factor of ten per decimal with nothing
else able to see it — so the market stops being lendable until it is fixed.
`unit_price` is the price with those precisions divided back out, per whole unit
of each asset, and is for display only.

### `GET /v1/assets`

`{"assets": {"<asset id>": {"ticker": "GOLD", "name": "Gold", "precision": 8}}}`
— the asset registry as this book resolved it, with the node's own asset labels
underneath. Refreshed every ten minutes.

### `GET /v1/fees`

The node's fee exchange rates, so a page can price a fee in whatever the wallet
holds:

```json
{
  "rates": {"<asset id>": 100000000},
  "relay_floor_rfa_per_kvb": 100,
  "rate_scale": 100000000,
  "feerate_rfa_per_kvb": 2000,
  "dust_relay_rfa_per_kvb": 100,
  "dust_output_vsize": 145,
  "vsize": {"repay": 2000, "repay4": 600, "take": 3000}
}
```

`rate_scale`, `dust_relay_rfa_per_kvb` and `dust_output_vsize` MIRROR the
node's policy: they are constants in `pignus/fees.py`, not values read back
from the node, and they are published here because the browser prices a fee and
folds change to dust from exactly them. Writing them down a second time in JavaScript
would give two copies of the node's arithmetic to drift apart the day its
policy changes, with neither side noticing.

`rates` is keyed by asset id and is empty when no node is configured.
`relay_floor_rfa_per_kvb` is `null` when the node did not report one, rather
than a guess presented as the node's number. Any asset with a rate can pay the
fee; none is privileged, and a fee is committed in the asset it is paid in —
`atoms = ceil(rfa × 1e8 / rate)`, so a more valuable asset pays fewer atoms.

`feerate_rfa_per_kvb` is what a composer should charge, well above the relay
floor on purpose: a fee at exactly the floor stops relaying the moment its
asset's rate drifts down between composition and inclusion. `vsize` is a
conservative size estimate per flow, keyed by the name the composer asks under;
the table is `VSIZE` in `pignus/fees.py`, and a flow it does not name has no
estimate to price against. A rate is reference units per whole asset unit,
scaled by `1e8`, which is what makes the formula above dimensionally sound.

### `GET /v1/vectors`

`pignus/vectors.json`, parsed and re-serialised. The browser rebuilds every
covenant address from these before it derives anything a user will act on, and
refuses to run if its own build does not match. 500 with an explanation if the
file is missing.

### `GET /v1/oracle`

```json
{"oracle_x": "…64 hex…", "url": "http://127.0.0.1:8740",
 "note": "verify every attestation against the key the VAULT bakes in, not against this one"}
```

The note is the API's own health warning and is served with it deliberately.
`url` is the primary's public address, the first entry of `oracle_public_urls`
in the book's configuration; only when none is configured is it the address
the book itself reads from, which behind a proxy is loopback.

### `GET /v1/oracles`

`{"oracles": ["…x-only hex…"], "urls": ["https://…"], "previous": ["…"],
"compromised": ["…"]}` — every independent oracle this book quotes against, in
configured order, primary first. A lender picks an m-of-n subset from here.
`previous` lists keys those oracles used to sign with, so a loan baked to one
reads as rotated rather than as a stranger's; `compromised` lists keys any of
them has declared compromised, which this book accepts nothing from, and
every loan or offer view carries `oracle_compromised` when it bakes one.

`urls` are the PUBLIC addresses, from `oracle_public_urls` in the book's
configuration, and are `""` for an oracle that has none. They are not the
addresses the book itself uses, which are typically loopback: answering "where
is this oracle" with `127.0.0.1` sends a reader to their own machine. The
address matters because a seizure is meant to be checkable by anyone — the
attestation behind it is at that oracle's `/v1/seizures` — and an m-of-n
seizure is signed by oracles that are not the primary.

### `GET /v1/attestation/{market}`

The primary oracle's latest verified attestation for a market. Write the market
with `_` for the slash: `/v1/attestation/GOLD_USDX`. `?oracle=<x-only hex>`
returns that particular signer's instead, which is how a loan baked to a key
this book no longer calls primary is still priced.

The attestation's own fields — `market`, `feed_id`, `timestamp`, `price`,
`price_scale`, `signature` — are passed through untouched. Only `feed_id`,
`timestamp` and `price` are inside the signature. `price_scale` is not: it is
baked into the vault's leaf instead, which is why a price quoted at one scale
and read at another is a hundredfold error the covenant cannot notice, and why
every consumer here compares the scale to the loan's own before using a price.

`oracle_x`, `age` (seconds) and `stale` are added beside them. Age is not a
signed property and cannot be, but a reader acting on a price needs it: a
signature stays valid however old the number under it is. It is SIGNED -- a
negative age is an attestation dated ahead of this book's clock, which is
exactly the case a reader most needs to see, and `stale` is true whenever the
attestation is not current: older than `max_price_age`, or dated more than two
minutes ahead of this book's clock.
Verify against the key the **vault** bakes in, not against whichever oracle
served it. 404 when the book holds none.

### `GET /v1/attestations/{market}`

`{"market": "GOLD/USDX", "attestations": [ … ]}` — one attestation per oracle
that has a verified one, each in the shape above, carrying the `oracle_x` it was
signed by along with its `age` and `stale`. This is what a threshold loan's
witness is assembled from, and what a liquidator uses to find the attestation
signed by the key a particular loan bakes in.

### `GET /v1/stats`

```json
{"loans_by_state": {"LIVE": 12, "REPAID": 40},
 "offers": 7, "offers_all": 31, "unreadable": 0,
 "live_debt_by_asset": {"<asset id>": "150000000000"},
 "at_risk": [{"loan_id": "…", "market": "GOLD/USDX", "health": 1.02,
              "liquidatable": false, "liquidatable_since": null}]}
```

`at_risk` lists LIVE loans whose health is under 1.15, weakest first, each
priced by the keys baked into its OWN vault -- not by whichever oracle this
book currently calls primary, which is a number a loan built on a rotated key
or on a threshold set cannot be judged by. A loan whose own oracle has no fresh
price is left out rather than shown at a health of zero, which would read as
"about to be liquidated".

`offers` counts the OPEN ones, the same figure `/healthz` reports; `offers_all`
counts every offer still recorded, spent and withdrawn included. `unreadable`
counts LIVE records this book could not parse, which are skipped here as they
are in `/v1/loans` rather than failing the whole read.

### `GET /v1/outpoint/{txid}/{vout}`

What an outpoint holds right now, mempool included, read with `gettxout` so no
transaction index is needed:

```json
{"txid": "…", "vout": 0, "scriptPubKey": "…hex…",
 "asset": "…64 hex…", "value": "100000000", "confirmations": 3}
```

- 400 if the txid is not 64 hex or the vout is not a number, if the node refuses
  the lookup, or if the output is confidential (it has no public amount, and a
  covenant cannot spend one).
- 404 if the output is unspent nowhere — it may already be spent.
- 429 if this client is reading the chain too fast.
- 503 if this book has no node.

### `GET /v1/scan/{scriptPubKey}`

Is there an unspent output at this address? `?asset=` (64 hex) and `?amount=`
(atoms) narrow it, and the newest match is returned.

```json
{"found": true, "scriptPubKey": "…hex…",
 "txid": "…", "vout": 0, "asset": "…64 hex…",
 "value": "4160000000", "confirmations": 3}
```

`{"found": false, "scriptPubKey": "…"}` when there is none. This is the one
question a browser cannot answer for itself and must be able to: a payment
whose report was lost — the relay down, a tab closed between broadcasting and
reporting — is invisible to a page rebuilding from this book, and the page
would then make the same payment twice. Every address here is derived from
terms the asker already holds, so asking about one reveals nothing.

It walks the whole UTXO set, so it is rationed like `/v1/spend` (429 when a
client asks too often), 400 if the scriptPubKey is not hex of 2 to 200
characters, `asset` is not 64 hex, `amount` is not a whole number, or the node
refuses the scan, and 503 when this book has no node.

### `GET /v1/spend/{txid}/{vout}`

Who spent an outpoint, and what their witness published. This is how a borrower
recovers the secret that releases their Bitcoin collateral without depending on
anybody telling them: a lender who claims a repayment publishes the preimage
whether they mean to or not, because the covenant leaf forces it into the
witness.

```json
{"txid": "…", "vout": 0, "spend_txid": "…",
 "confirmations": 12, "anchor_confirmations": 3,
 "preimages": {"<sha256 of the item>": "<32-byte item, hex>"}}
```

`anchor_confirmations` is how deep the PARENT chain is behind the Bitcoin block
that the spend's own Sequentia block anchored to, and it is the number to act
on. Sequentia reorgs whenever Bitcoin reorgs, so `confirmations` measures the
wrong thing: six of them are six minutes, about six tenths of one Bitcoin
block, and one ordinary single-block Bitcoin reorg undoes ten at once. It is
`null` when this book has no Bitcoin node, when the spend is unconfirmed, or
when the anchor is not in the parent chain this book follows — and `null` means
"not established", never "safe".

`preimages` is every 32-byte witness item keyed by its SHA-256, so a caller
picks by the hash its own loan commits to and this book needs to know nothing
about that loan. The mempool is searched first, then blocks backwards from the
tip as far as `back_scan_cap`.

- 400 if the txid is not 64 hex or the vout is not a number.
- 404 if the output is still unspent, or its spend is outside the scan window.
- 429 if this client is reading the chain too fast.
- 503 if this book has no node.

### `GET /v1/btc/outpoint/{txid}/{vout}`

Is a **Bitcoin** outpoint still unspent, and if not, what spent it:

```json
{"txid": "…", "vout": 0, "unspent": false, "spend_txid": "…",
 "witness": ["…", "…"], "confirmations": 12}
```

`{"txid": "…", "vout": 0, "unspent": true, "confirmations": n}` while it is
still there. `unspent` false with an empty `spend_txid` means the coin is gone
but its spend is outside this book's scan window (`back_scan_cap` blocks):
treat that as "taken by something unseen", never as unspent. This book stays
ignorant of any loan, exactly as `/v1/spend` does: it returns the outpoint's
state and the raw witness, and the caller names the leaf from its own copy of
the tree.

It exists because a cross-chain vault has three leaves and two of them are the
lender's — SEIZE, which they and the oracle sign together with no price test in
any script, and TIMEOUT. Either empties the vault at a moment nobody tells the
borrower about, so a page that cannot read this shows a seized loan as live,
with a Repay button, and the borrower pays a debt for collateral that was taken
before they paid it.

- 400 if the txid is not 64 hex or the vout is not a number.
- 503 if this book has no Bitcoin node.
- 429 if this client is reading the chain too fast.

### `GET /v1/unrenderable`

The records this book holds and cannot show, and why:

```json
{"unrenderable": {"<offer or loan id>": "ValueError: debt must be at least 1 atom"}}
```

A record that will not parse is missing from every listing, so a lender looking
for their own offer simply does not find it. `/healthz` counts them and turns
unhealthy; this says which, so an operator can look at the record rather than
at the whole book.

### `GET /healthz`

```json
{"ok": false,
 "error": "stale price: SILVR/USDX",
 "version": "0.2.0", "git_rev": "0aa3fbb1",
 "covenant_vectors": 15,
 "height": 118432, "last_poll": 1799999950,
 "markets": 6, "priced": 5, "stale_markets": ["SILVR/USDX"],
 "max_price_age": 600, "min_depth": 2,
 "rescan_depth": 1500, "back_scan_cap": 200, "prune_after": 2592000,
 "offers": 7, "loans": 52, "unrenderable": 0,
 "explorer_url": "", "oracle_public_url": "",
 "assets": 41, "fee_rates": 6,
 "block_seconds": 60, "reference_ticker": "USDX",
 "oracles": 3, "oracle_errors": [],
 "event_errors": [], "event_backlog": 0,
 "rescan_needed_from": null,
 "node": true, "btc_node": true, "btc_height": 155377,
 "compromised_keys": []}
```

`rescan_needed_from` is a height when this book was stopped for longer than
`rescan_depth` blocks: a poll's backward walk is bounded, so what happened in
between is invisible until an operator runs it once with `--rescan-from` that
height, and until then offers and vaults that moved meanwhile read as gone or
unknown. The book keeps its own last reconciled height in `book.json` (under
`meta`) to know. It is `null` otherwise, and `ok` is false while it is set.

`ok` is false when there is no node, when the node or the **primary** oracle is
unreachable, while the first sync is still running, when the poll thread has not
finished within `max(120s, 3 × poll)`, when any market's newest verified
attestation is older than `max_price_age`, when offer events are queued up
unapplied, when the last poll step failed, when a market is priced but not
lendable (its tickers do not resolve to assets, or their precisions disagree),
when any record cannot be rendered, when a configured Bitcoin node is not
answering (cross-chain deadlines go unchecked and the page will not
originate), when the node stopped answering partway
through the last poll (the rest of it is abandoned rather than waited out,
one RPC timeout per record), or while `rescan_needed_from` is set.
`error` says which; `stale_markets`, `oracle_errors` and
`event_errors` name them individually. The status code is always 200 — read
`ok`, not the code.

`btc_node` says whether this book can see the parent chain, and `btc_height` is
that chain's tip. Without them a cross-chain loan's Bitcoin-side deadlines are
not checked here at all, and every take this book accepts says so in its
warnings — so a page refuses to originate one, because it could not tell a
borrower when their collateral becomes abortable.

`event_backlog` is how many offer events are waiting to be applied, and
`event_errors` the last few this book dropped because it could never apply
them. A backlog that only grows means loans an offer opened are not being
registered.

`covenant_vectors` counts the golden vector cases the covenant tripwire checked
in this process. Zero would mean the builder was never loaded; a non-zero count
is an operator's proof that the check ran. `git_rev` and `version` say which
checkout is answering, which is how a box that lags the repository is caught.

## Offers

An offer is **funded**: the lender's principal already rests in an offer
covenant that any borrower may take unilaterally, in one transaction that locks
a correctly shaped vault. So an offer has a coin, and this book refuses to list
one it cannot find on chain.

### `GET /v1/offers`

| parameter | default | meaning |
|---|---|---|
| `market` | every market | `GOLD/USDX` |
| `status` | `open` | `open`, `delisted`, `taken`, `withdrawn`, `gone`, `ghost`, `expired`, or `all`. `expired` is derived from the chain rather than stored, so it answers with the open offers whose expiry has passed. A value outside that set is a 400 listing them, never an empty result |
| `limit` | the whole book | how many of the newest to return |

`{"offers": [ … ]}`, newest first. A stored record that cannot be rendered is
skipped rather than losing the whole list; `unrenderable` in `/healthz` counts
them. `limit` must be a non-negative number, or the answer is 400.

`gone` means the offer's coin was spent by something this book could not name;
`ghost` means its funding was undone by a Bitcoin-driven reorg. A ghost is kept
and watched, because the funding transaction is still valid and is normally
mined again, and the offer reopens when it is.

### `GET /v1/offers/{id}`

One offer. `/v1/offer/{id}` is accepted as an alias. 404 if there is none.

```json
{
  "offer_id": "…32 hex…",
  "terms": "{…}",
  "kind": "funded",
  "outpoint": "<txid>:<vout>",
  "vault_address": "…scriptPubKey hex…",
  "market": "GOLD/USDX",
  "principal": "10000000000", "collateral": "6666666666",
  "expiry_locktime": 119000,
  "funded_value": "30000000000", "confirmations": 12,
  "lots_left": 3,
  "collateral_asset": "…", "debt_asset": "…",
  "collateral_ticker": "GOLD", "debt_ticker": "USDX",
  "collateral_precision": 8, "debt_precision": 8,
  "debt": "10500000000", "strike": "18000000", "maturity": 119000,
  "lender_prog": "…hex…", "lender_ver": 1, "oracle_x": "…",
  "oracle_compromised": false,
  "price": 300000000, "open_ltv": 0.5,
  "expired": false, "height": 118432,
  "warnings": [],
  "status": "open", "created": 1799990000
}
```

`lots_left` is what the coin still holds divided by one principal, so an offer
partly taken advertises what is left. `price` and `open_ltv` appear only when
there is a fresh price under the oracle keys these terms bake in, by the same
rule as a loan's. `manage_hash` is never served.

### `POST /v1/offers`

Publish a funded offer. Only these fields are read, and everything else in the
body is dropped rather than stored and served back as though the book had
checked it:

| field | required | meaning |
|---|---|---|
| `terms` | yes | the loan terms document |
| `kind` | yes | must be `"funded"` |
| `outpoint` | yes | `"<txid>:<vout>"`, where the principal rests |
| `principal` | no | atoms per lot; defaults to the terms' principal |
| `collateral` | no | atoms a lot locks; defaults to the terms' amount |
| `expiry_locktime` | no | defaults to the terms' maturity |
| `manage_token` | no | only to re-publish a listing you already own |

The book checks the coin before it will list it: the outpoint must exist, hold
an explicit (unblinded) amount, pay the offer address these terms compile to,
carry the debt asset, and hold at least one principal.

**The offer id is derived** from the terms and the outpoint. A body that names
its own `offer_id` is ignored — an id a publisher may choose is an id anyone may
choose, and publishing under someone else's would replace their record and their
manage token with it.

The response is the offer view plus `manage_token`, returned **once**, to the
publisher. Keep it: it is what cancels the listing later. The book stores only
its hash.

- 400 if the terms will not build, the body is missing a field, or the chain
  contradicts the offer (`kind` not funded, no outpoint, the coin is confidential,
  the wrong address, the wrong asset, or less than one principal).
- 409 if that coin is already listed on those terms and the request did not
  carry that listing's manage token. Re-publishing with the token refreshes the
  record and keeps its id, its token and its age.
- 400 if this book has no node: it will not publish an offer it cannot check.
- 429 if this client is writing too fast, or if the book already holds
  `MAX_OFFERS` (2000) open listings.

### `DELETE /v1/offers/{id}`

Take a listing off the board. The record is KEPT, marked `delisted`: it is the
only copy of the terms that can ever spend the coin, because the offer address
is their hash and nothing on chain carries them, so a book that forgot it would
be forgetting the lender's principal. A delisted offer is absent from
`GET /v1/offers`, listed by `?status=delisted`, still served by `GET
/v1/offer/{id}`, and still watched, so the withdraw at expiry works exactly as
for one that expired on the board. Publishing the same coin again reopens it.
The coin is untouched throughout — the coin is the truth, and the lender
withdraws it with `pignus-cli offer-withdraw` or the page's Withdraw button.

The manage token goes in the `X-Manage-Token` header, or as `?token=` if a
header is impossible. The query form is redacted from the log; the header is the
right place for it.

`{"removed": true, "note": "…"}` on success. 404 if there is no such listing,
403 without the token, 429 if this client is writing too fast.

From the command line: `pignus-cli offer-delist --offer <id> --token <token>`,
which sends the token in the header.

## Loans

A loan is a vault on chain. The book usually discovers one by itself — an
offer's coin is watched, and when it is taken the borrower's payout program is
read out of the take witness and the new vault is registered — so a loan taken
by any wallet, through the page or not, turns up here without being told, as
long as the take is within the watcher's scan depth.

### `GET /v1/loans`

| parameter | default | meaning |
|---|---|---|
| `state` | every state | one of the states below |
| `market` | every market | `GOLD/USDX` |
| `oracle_x` | every loan | only loans that bake this x-only key, which is how the loans against a retiring oracle are listed during a rotation |
| `limit` | the whole book | how many of the newest to return |

States: `UNCONFIRMED` (funding seen, not yet in a block), `LIVE` (funded to at
least `min_depth` and unspent), `REPAID`, `LIQUIDATED`, `DEFAULTED`, `RECOVERED`
(closed by that leaf), `SPENT_UNKNOWN` (spent by a witness this watcher could
not name) and `GHOST` (the funding was undone by a Bitcoin-driven reorg).

A misspelled `state` is a 400 whose body lists all eight. An empty list would
read as "you have no loans" rather than "that is not a state", which is the one
answer a caller cannot tell from the truth.

### `GET /v1/loans/{id}`

One loan, with its health at the current price. `/v1/loan/{id}` is an alias.

```json
{
  "loan_id": "…", "terms_id": "…",
  "terms": "{…}",
  "state": "LIVE", "confirmations": 143,
  "txid": "…", "vout": 0,
  "single_leaf": true,
  "vault_address": "…scriptPubKey hex…",
  "market": "GOLD/USDX",
  "collateral_asset": "…", "debt_asset": "…",
  "collateral_ticker": "GOLD", "debt_ticker": "USDX",
  "collateral_precision": 8, "debt_precision": 8,
  "principal": "10000000000", "debt": "10500000000",
  "collateral_amount": "6666666666",
  "strike": "18000000", "price_scale": 100000,
  "maturity": 119000, "recover_after": 162200,
  "lender_prog": "…", "lender_ver": 1,
  "borrower_prog": "…", "borrower_ver": 0,
  "oracle": "2-of-3", "oracle_x": "…",
  "oracle_keys": ["…", "…", "…"],
  "oracle_compromised": false,
  "height": 118432, "past_maturity": false, "recover_open": false,
  "price": 300000000, "health": 1.6667, "ltv": 0.525,
  "liquidatable": false,
  "seizure_if_liquidated": "3675000", "surplus_if_liquidated": "6662991666",
  "spent_by": "", "spent_height": 0, "closed_confirmations": 0, "note": "",
  "min_depth": 2,
 "funding_height": 118289, "funding_block": "…"
}
```

`liquidatable_since` is the Unix time at which a LIVE loan's price last crossed
under its strike, present only while it is under; the book stamps the crossing
on every price refresh and clears it when the price climbs back. With no
liquidator guaranteed to be running it is the difference between "just crossed"
and "liquidatable for three hours and nobody has". `/v1/stats` carries it on
each `at_risk` row beside `liquidatable`.

`funding_height` and `funding_block` are how a Bitcoin-driven reorg is told from
a spend the watcher could not reach, and they are persisted with the record so a
restart does not lose the distinction. `closed_confirmations` is how deep the
CLOSE is, which is a different question from how deep the funding was: a
repayment or a liquidation one block old can still be reorged out, and zero
means either not closed at all or closed only in the mempool. `min_depth` is the
number both are counted towards, repeated on the loan so a reader is not made to
fetch `/v1/markets` to learn what it is. It is a display threshold and nothing
more: the watcher goes on re-reading a closed vault's output every poll until
the close is `rescan_depth` blocks deep, because Sequentia reorgs whenever
Bitcoin does and a Sequentia depth is never finality. A close undone by a reorg
reopens the loan, however deep the book had shown it.

`single_leaf` says which vault layout this loan lives in: a loan originated
through a funded offer is in the single-leaf vault, a directly originated one is
in the four-leaf tree, and the two have different addresses and different
witnesses. `vault_address` is the scriptPubKey for THAT layout -- the one this
loan's coin actually pays -- and it is the thing to compare against the coin
before signing anything. On an offer it is likewise the single-leaf address,
because that is where a loan drawn from the offer will live.

The price-derived fields — `price`, `health`, `ltv`, `liquidatable`,
`seizure_if_liquidated`, `surplus_if_liquidated` — are **absent** when there is
no price this loan can be judged by. Their absence is the signal; there is no
stale price dressed as a live one.

`price` is not the book's headline price. A vault verifies against the keys
baked into it, so a loan is priced only from attestations by **its own** oracles,
at **its own** `price_scale`, timestamped at or after its own `not_before`, and
only when at least `threshold` of them qualify. The number shown is then the
`threshold`-th lowest of those, because the covenant takes the maximum of the
prices presented and the best a spender can present is the `threshold` lowest.
A loan left behind by a key rotation is therefore shown at its old oracle's
price or at none, never at a number the covenant would refuse.

### `POST /v1/loans`

Register a loan for watching. Body: `terms`, `txid`, `vout`, optionally
`single_leaf` (default false, the four-leaf tree) and `offer_id` (kept only if
that offer is in this book).

The book checks the vault before it will track it: the outpoint must pay the
address these terms compile to in the layout named, hold an explicit amount, and
hold **exactly** the collateral the terms say — not at least, because the
covenant returns what is locked, and terms that overstate the coin misprice
every figure derived from them.

The state is **never** taken from the request. A loan is not originated until
its funding is buried, and a caller who could claim `LIVE` at zero
confirmations would say so on everyone's screen. The watcher decides, from the
chain, in the refresh that runs before the response is written.

The response is the loan view. 400 for a malformed body or a chain that
contradicts it; 429 for too-fast writes, for a book already holding `MAX_LOANS`
(5000) open loans, or when this client already has 20 registrations still
waiting for their funding to bury. That last cap exists because a loan may be
registered on a mempool transaction the caller then double-spends: each of those
leaves a ghost behind and costs the caller nothing on chain, so the number of
them one client may have in flight is the only thing bounding it.

### `GET /v1/loans/{id}/exit`

How a closed loan ended, read back off the chain: the exit leaf that was
revealed, the oracle evidence in the witness — checked against the key the vault
itself bakes in — and what the transaction actually paid beside what the terms
say that price buys.

```json
{"loan_id": "…", "exit": "LIQUIDATED", "spent_by": "…", "input_index": 0,
 "height": 118500, "market": "GOLD/USDX",
 "strike": 18000000, "price_scale": 100000, "not_before": 1799000000,
 "maturity": 119000, "debt": "10500000000", "collateral": "6666666666",
 "attestations": [{"oracle_x": "…", "price": 17500000,
                   "timestamp": 1799999940, "signature": "…",
                   "present": true, "verified": true}],
 "price_used": 17500000,
 "seize_expected": "3675000", "seize_paid": "3675000",
 "surplus_expected": "6662991666", "surplus_paid": "6662991666",
 "lender_paid": "10500000000",
 "problems": []}
```

`strike` and `not_before` are JSON numbers here, unlike `strike` on
`/v1/loans/{id}`; the amounts -- `debt`, `collateral`, `seize_*`, `surplus_*`,
`lender_paid` -- are decimal strings.

`problems` is empty when everything the covenant enforces is visible and agrees;
a non-empty entry names what could not be checked or did not add up. A borrower
who was liquidated has no other way to see the price it happened at.
`pignus-cli explain --loan <id>` prints exactly this, and works from a terms
file and a txid with no book involved.

Two cases answer 200 with a short body rather than the whole account: the
closing transaction is in neither the block this book recorded nor the mempool,
and the transaction found there does not spend this vault. Both carry `loan_id`,
`spent_by`, `height`, `exit` and a one-line `problems`, and nothing else. They
are answers rather than errors — what the book knows, and why it cannot say
more — so a reader that checks `problems` before the numbers handles them
alongside a complete account.

- 404 if there is no such loan, or it is still open, or this book never saw it
  close.
- 503 if this book has no node: a close cannot be read back without one.

## The cross-chain relay

A Bitcoin-collateral loan needs a lender who is present: Bitcoin has no
covenants, so origination is a two-party exchange and liquidation needs the
oracle to co-sign. Something has to carry messages between two parties who are
not both at a keyboard, and these endpoints are that something.

**The relay is never trusted.** It holds no key and can move nothing. Every
message a lender's responder will act on carries a BIP340 signature by the key
the loan already names, over a tagged hash of exactly the fields that matter
(`pignus/btc_relay.py`); the relay verifies before storing and the responder
verifies again before acting. What is left for the relay to be wrong about is
availability, which is the one thing a relay is allowed to be wrong about.

The relay also recomputes rather than believes: it rebuilds the vault outpoint
from the offer's own terms, rebuilds the reclaim sighash, and verifies both the
borrower's advance signature and the lender's release against them before
storing either.

**The handshake**, in order: `POST /v1/btc/take` (a borrower asks),
`POST /v1/btc/hash` (the lender draws this loan's secret and publishes its
hash), `POST /v1/btc/presig` (the borrower signs the one transaction that can
move their collateral into the vault that hash implies) and
`POST /v1/btc/adaptor` (the lender returns the release). Only after checking
that release does the borrower broadcast anything. Everything past that point is
a report of something already on a chain: `/disbursed`, `/claimed-principal`,
`/upgraded`, `/repaid`, `/claimed`, `/refunded`.

**Take statuses**, in order: `requested` (the borrower has asked; the lender has
not drawn this loan's secret yet, so there is no vault), `reserved` (the hash is
published and the vault is derivable), `pending` (the borrower's advance
signature is stored, and the lender's release is what is waited on), `signed`
(the release is stored), `disbursed` (the principal is paid into the hashlock),
`live` (the collateral is in the vault), `claimed` (the lender has taken the
repayment, publishing `t`), `refunded` (the lender took an unclaimed principal
back).

Every one of those is set by the LENDER's own signed report, or by the
handshake. A borrower's reports set no status at all: they record where a
payment landed and nothing else. That is deliberate, and it is the reason a
lender's responder decides what to do from its own records and the two chains
rather than from a status here. A status a borrower could set is a status they
could use, to move a take out from under the step the lender was about to take
-- leaving a principal paid and collateral never vaulted -- and to pin a
lender's lot in a state nothing would ever clear.

So a client watching a loan should read the FIELDS, not the status word:
`disbursement_txid` says the principal was paid, `principal_claim_txid` that
the borrower took it, `upgrade_txid` that the loan started, `repay_txid` that
the debt was paid, `claim_txid` with `secret_t` that the lender took it, and
`refund_txid` that an unclaimed principal went home. Each only ever becomes
true. The page derives everything it shows from them.

`lots_left` on an offer is what a take holds against it. A take that is still
`requested`, `reserved` or `pending` five minutes after it was made releases
its lot, and a `signed` one whose collateral never
appeared releases its lot after six hours: a borrower who asks and walks away
must not hold a lender's offer shut. `disbursed`, `live` and `claimed` hold
their lot for good, because money is in flight by then; `refunded` releases
it, since the principal went home.

Those windows are short on purpose. Asking for a loan is free and anonymous, so
every second an unfinished request holds a lot is a second somebody who is not
lending anything has closed a lender's offer. The handshake it waits for takes
seconds when a responder is running, and the write rate limiter bounds how fast
one client can make requests at all.

### `GET /v1/btc/offers`

`?status=` takes the same set as `/v1/offers` -- `open` by default, or
`delisted`, `taken`, `withdrawn`, `gone`, `ghost`, `expired`, `all` -- and a value outside it is a
400 listing them, not an empty result, because an empty list reads as "there are
no offers". Only three of those are ever ASSIGNED to a cross-chain offer:
`open`, `withdrawn` when its lender takes it down, and `expired` when its own
deadlines have gone by. The rest describe a coin on a chain, and a cross-chain
offer has none.

Each row is the stored offer plus `lots_left`, computed live: how many of its
lots are still free, once the takes holding one are counted. A borrower reads
that before choosing an offer, since a lot somebody else is part-way through is
not one they can have.

A cross-chain offer carries no coin, so nothing on a chain ends one. The book
ends it instead: an offer whose own four deadlines no longer leave both sides
the margins a take is checked against becomes `expired` on the next poll, and
is pruned with the other dead records. That is the same rule that would refuse
the take, applied one step earlier -- and it is what keeps the ceiling on open
offers from being a one-way door, since publishing one costs nothing but a
self-signature.

```json
{"offers": [{
  "btc_offer_id": "…24 hex…",
  "loan": {"btc_amount": "100000", "lender_x": "…", "oracle_x": "…",
           "recover_after": 900000, "debt_asset": "…", "debt": "5250000000",
           "principal": "5000000000", "repay_deadline": 125000,
           "abort_after": 902000, "upgrade_fee": 10000, "d_refund": 124000,
           "lender_prog": "…", "lender_ver": 0,
           "market": "BTC/USDX", "strike": "4200000000", "price_scale": 100000},
  "market": "BTC/USDX", "lots": 3, "lots_taken": 1, "lots_left": 2,
  "offer_sig": "…128 hex…", "responder": "", "note": "",
  "status": "open", "created": 1799990000}]}
```

### `GET /v1/btc/offer/{id}`

One offer, with `lots_left`. 404 if there is none.

### `POST /v1/btc/offers`

A lender publishes an offer. Body: `loan`, `market`, `lots`, `offer_sig`, and
optionally `responder` and a `note` of up to 200 characters.

Thirteen fields of `loan` are required and must be non-empty: `btc_amount`,
`lender_x`, `oracle_x`, `recover_after`, `debt_asset`, `debt`,
`repay_deadline`, `abort_after`, `d_refund`, `lender_prog`, `upgrade_fee`,
`market` (the loan's own copy, beside the sibling `market`) and `strike`. The
rest of the fields shown above are optional; the borrower's own -- `borrower_x`
and `h_w` -- belong to a take and are never in an offer.

`btc_amount`, `debt`, `principal` and `strike` are decimal strings, for the
reason every other amount on this book is one. The rest of the loan -- heights,
locktimes, `upgrade_fee`, `price_scale`, `lender_ver` -- are JSON numbers. The
book accepts either spelling and stores this one, and the digest under
`offer_sig` is computed over it, so a lender signing from integers and a relay
verifying from strings agree. An absent number counts as a zero there, not as
an empty string.

`offer_sig` is the lender's BIP340 signature over the offer's own terms. It is
what a responder checks before acting on any take of the offer, together with
the id: an offer this relay serves under an id that is not `offer_id` of its
terms is refused by the responder, since a lot cap counted per id would count a
single offer served under two ids twice. `offer_sig` is
what makes this endpoint safe to leave open: an offer carrying somebody else's
key would make **their** responder pay it out.

`upgrade_fee` must be at least 10,000 satoshis. The transaction it pays for is
signed in advance by both parties, spends a covenant leaf and sets a final
sequence, so neither side can replace it or pay for a child: whatever is
committed at origination is the only fee it will ever have. Every party checks
this, not only this book, because a loan arranged by hand never passes a relay.

`repay_deadline` is not the moment a borrower is held to. A lender stops
claiming 120 Sequentia blocks -- two hours -- before it, because claiming
publishes the secret and doing that as the borrower's own refund opened would
hand them the debt and the collateral both. So the deadline to show a borrower
is `repay_deadline - 120`, and a repayment made after it is one nobody will
answer.

The id is derived from the signed terms, so republishing the same offer is
idempotent — it keeps the record's age and what has already been taken — and two
different offers can never collide.

- 400 if a required field is missing or a payout program's length does not match
  its witness version (20 bytes at v0, 32 at v1).
- 403 if the signature is not by the key the offer names as the lender.
- 400 if `strike` is not positive, or `oracle_x` is the lender's own key.
- 400 if the four deadlines leave no room, judged against both chains' tips
  the way every responder judges them; the message names each margin that is
  short. Only when this relay has both nodes.
- 429 if the book already holds 500 open cross-chain offers. Republishing one it
  already has is never refused.

### `POST /v1/btc/offers/{id}/withdraw`

Body: `{"sig": "…"}`, the lender's signature over the withdrawal. Sets the
offer's status to `withdrawn`. 404 for an unknown offer, 403 if the signature is
not the publishing lender's.

### `POST /v1/btc/offers/{id}/resign`

Body: `{"offer_sig": "…"}`, a fresh signature over the terms this book already
holds for that offer. Nothing else changes -- not a term, not the id, not the
status -- and the book verifies the signature over the stored payload, under
the key those terms name, before it accepts one. So it can only ever replace a
signature that does not check out with one that does, and nobody without the
lender's key can use it.

It exists because an offer whose signature stops verifying stops every loan
under it. A lender's responder checks that signature before acting on any take,
so a live loan under a disowned offer is one whose repayment is never claimed
and whose collateral is never released -- and the responder cannot tell that
apart from having no work to do. `pignus-cli btc-responder-status --book …`
names such an offer and exits 4; `pignus-cli btc-offer-resign --offer <id>`
repairs it.

404 for an unknown offer. 403 if the signature does not check out over the
stored terms.

### `POST /v1/btc/take`

A borrower asks for a loan. This is a request rather than a take: the vault the
collateral will end up in commits to the hash of a secret the lender has not
drawn yet, so neither the vault nor the release over it exists at this point.

```json
{"btc_offer_id": "…", "borrower_x": "<32-byte hex>",
 "h_w": "<32-byte hex>", "w_seq": 0,
 "borrower_prog": "<20 or 32-byte hex>", "borrower_ver": 0,
 "borrower_seq_spk": "0014…",
 "prevault_txid": "…", "prevault_vout": 1, "prevault_value": "103000",
 "reclaim_dest": "0014…", "reclaim_fee": 3000,
 "take_auth": "…128 hex…"}
```

The relay rebuilds the loan from the offer's own terms plus the four things a
taker chooses (`borrower_x`, `h_w`, `borrower_prog`, `borrower_ver`) and
refuses the request unless the pre-vault address and value it derives match the
outpoint named. It also refuses an outpoint another take already names: one coin
funds one loan.

`take_auth` is the borrower's BIP340 signature, with the key `borrower_x`, over
the tagged hash `pignus/btc-take/1` of the canonical JSON (sorted keys, no
spaces) of `btc_offer_id`, `borrower_x`, `h_w`, `borrower_prog`,
`borrower_ver`, `prevault_txid` and `prevault_vout` -- the strings lower-cased,
the two numbers as JSON numbers. It is required, and it exists for the seizure
a lender may one day ask for: the strike is in no Bitcoin script, and the
lender's own signature over the offer can be made again over any strike, so
the borrower's signature over the id of the offer as it was when they took it
is the one thing that pins the strike a seizure is judged against. The relay
stores it on the take and serves it back.

`200` returns the take with `status: "requested"`, its derived `prevault_spk`
and `disbursement_spk`, and any `warnings` its deadlines raise. `404` no such
offer. `409` the offer is closed, or every lot is spoken for. `400` anything the
relay could not rebuild, including an outpoint another take already names.
`429` this client is writing too fast.

### `POST /v1/btc/hash`

The lender draws this loan's secret and publishes its hash. One secret per loan:
sharing one across an offer's takes would let any borrower's repayment release
every other borrower's collateral.

```json
{"take_id": "…", "payment_hash": "<32-byte hex>",
 "adaptor_point": "<32-byte hex>", "auth": "<64-byte hex>"}
```

`auth` is a BIP340 signature by the offer's `lender_x` over
`tagged("pignus/btc-hash/1", canonical({take_id, payment_hash, adaptor_point}))`.
The relay stores it and serves it back, so a borrower can check that the hash
their repayment will commit to came from the lender rather than from the relay.

`200` is `{"ok": true, "take_id": "…"}` and moves the take to
`status: "reserved"`, recording the `vault_txid` and the `repayment_spk` that
hash implies — read them back from `GET /v1/btc/take/{id}`. `403` the signature
is not the lender's. `409` the take already has a different hash, or this hash
is already committed to another loan on this book. `400` the hash is not 32
bytes, or is the one the principal is locked to (`h_w`). `404` no such take.

### `POST /v1/btc/presig`

The borrower signs the one transaction that can move their collateral into the
vault -- which they can only derive now that the hash exists.

```json
{"take_id": "…", "upgrade_presig": "<64-byte hex>"}
```

The relay verifies the signature against the loan it rebuilt, so a lender is
never asked to fund a loan that could not start. `200` moves the take to
`status: "pending"`. `409` the take has no hash yet. `400` the signature does
not move that collateral into that vault. `404` no such take.

### `GET /v1/btc/takes`

`?status=`, `?offer_id=`, `?borrower_x=`. `{"takes": [ … ]}`, newest first.
Asking by borrower key is what lets somebody who cleared their browser storage,
or moved to another machine, find their own loans again. What comes back is
the book's word, and the page treats it as that: a take it has no copy of is
kept only if `take_auth` verifies under the wallet's own key over the take it
describes, and if `btc_offer_id` is the hash of the terms the book serves for
that offer under the offer's own market and lot count. A record failing
either is left out, not repaired.

`?oracle_x=` narrows to the takes whose loan names one oracle key, which is what an
operator retiring a key checks before they stop the instance: on this tier the
oracle's signature IS the liquidation, so a live take still naming a retired key
has lost its lender's only way to seize.

### `GET /v1/btc/take/{id}`

One take. 404 if there is none. This is what the page polls while it waits for a
lender.

### `POST /v1/btc/adaptor`

The lender's release: an ordinary BIP340 signature over the transaction that
returns the collateral to the borrower, which the borrower can check for
themselves before committing anything.

```json
{"take_id": "…", "release_sig": "<64-byte hex>",
 "adaptor_point": "<32-byte hex>", "payment_hash": "<32-byte hex>",
 "auth": "<64-byte hex>"}
```

The field is served back as both `release_sig` and `adaptor_sig`, and either is
accepted on the way in. The value is an ordinary BIP340 release signature, not
an adaptor signature; the second name is an alias every client and relay
understands.

`auth` is a BIP340 signature by the offer's `lender_x` over
`tagged("pignus/btc-adaptor/1", canonical({take_id, adaptor_point,
payment_hash, adaptor_sig}))`.

The relay derives the vault from the take's own pre-vault outpoint and payment
hash, rebuilds the reclaim transaction, and refuses a signature that does not
verify against it. `200` is `{"ok": true, "take_id": "…"}` and moves the take
to `status: "signed"`, recording the `vault_txid` it derived -- read it back
from `GET /v1/btc/take/{id}`. `403` the reply is not the lender's. `409` the take has
no advance signature yet, the release is for a different payment hash, or a
different release is already stored (re-sending the same one is accepted).
`400` the signature does not verify.

### `POST /v1/btc/disbursed`, `/upgraded`, `/claimed`, `/refunded`

The lender's reports, each signed under its own tag over exactly the fields it
asserts. All take `take_id` and `auth`; each takes one or two more:

| endpoint | fields | new status |
|---|---|---|
| `/v1/btc/disbursed` | `disbursement_txid`, `disbursement_vout` | `disbursed` |
| `/v1/btc/upgraded` | `upgrade_txid` | `live` |
| `/v1/btc/claimed` | `claim_txid`, `secret_t` | `claimed` |
| `/v1/btc/refunded` | `refund_txid` | `refunded` |

`/v1/btc/claimed` publishes the secret that completes the borrower's release, so
the relay checks it: a `secret_t` whose SHA-256 is not the loan's `payment_hash`
is refused with 400 rather than published. It is reported twice -- once when the
claim is made, again with the secret once that claim is buried -- and the empty
first form never overwrites a secret already published.

`{"ok": true, "take_id": "…"}` on success. 403 if the report is not signed by
this loan's lender, 404 for an unknown take.

Each report's signature is KEPT, under the report's own name —
`disbursed_auth`, `upgraded_auth`, `claimed_auth`, `refunded_auth`, beside the
`hash_auth` and `adaptor_auth` from the handshake — and served with the take. A
borrower rebuilding a loan from this book can then check every step against the
lender's own key rather than believing the carrier: the outpoint a claim is
built on decides where the money comes from, so it is not a thing to take on
anybody's word. A report writes its FIELDS whatever the take's current status,
and moves the status only forward, so a lender whose report was refused once
can send it again.

### `POST /v1/btc/claimed-principal`, `/v1/btc/repaid`

The borrower's own reports: where they took the principal, and where they paid
the debt. Both are hints -- everything they say is on chain -- and both are
stored on the take (`/v1/btc/claimed-principal` as `principal_claim_txid`,
`/v1/btc/repaid` as `repay_txid` and `repay_vout`) and change NO status,
because a status a borrower can set is one
they can use to move a take out from under the step their lender was about to
take. They are signed by the key the take names all the same: an unsigned hint
is a way to write into somebody else's loan, and a lender's responder saves a
whole-UTXO-set scan by believing one that checks out.

```json
{"take_id": "…", "txid": "…", "vout": 0, "auth": "<64-byte hex>"}
```

`auth` is a BIP340 signature by the take's `borrower_x` over
`tagged("pignus/btc-claimed-principal/1" | "pignus/btc-repaid/1",
canonical({take_id, txid, vout}))`.

`200` is `{"ok": true}` on either. `403` the signature is not the borrower's.
`404` no such take.

The page sends both as it broadcasts. From the command line they are sent by
`pignus-cli btc-claim-principal` and `btc-repay` when given `--book` and
`--borrower-key`, and a failure to send one is printed and otherwise ignored:
the payment is on chain either way, and the report only spares the lender a
scan and keeps the borrower's own page current.

# `pignus-oracle`

Everything the oracle serves is a read. The ones that go to disk for it —
`/v1/log/raw`, `/v1/seizures`, `/v1/seizure/{sighash}`,
`/v1/attestation/{market}/at/{ts}`, and `/v1/log` whenever `since`, `until` or
`cursor` sends it to the archive — are limited to one request a second per
address with a burst of ten, and twenty a second with a burst of two hundred
for everybody together, and answer 429 over that. `/v1/attestation/{market}`
is limited the same way while the market has no attestation in memory and the
answer has to come from the archive; a book treats that 429 as the oracle
erroring, not as no price. A market found nowhere on disk is remembered as
missing for a minute rather than searched for again on the next request. An
auditor's query is
cheap once and expensive in a loop, and this process has signing to do.
Everything else comes out of memory and is not limited.

## Endpoints

### `GET /v1/pubkey`

`{"oracle_x": "…64 hex…", "price_scale": 100000, "previous": ["…"],
"compromised": ["…"]}` — the key vaults bake in. `previous` lists keys this
oracle used to sign with, so a borrower's page can tell a rotation from a
stranger; live vaults bake the key they were originated against, so a
rotation is never a swap. `compromised` lists keys this operator has declared
compromised: a book refuses attestations under them and flags the loans that
bake them.

### `GET /v1/markets`

```json
{"markets": [{"market": "GOLD/USDX", "feed_id": "…", "price_scale": 100000,
              "collateral_precision": 8, "debt_precision": 8, "error": null}]}
```

The precisions are the decimal counts this oracle signed the price with. Check
them against the registry: a wrong one signs a price wrong by a power of ten,
and no signature check downstream can catch that.

### `GET /v1/attestation/{market}`

The latest signed price, with `_` for the slash. The signed fields are
untouched; `age` (seconds), `stale` (true when the feed behind it is erroring)
and `error` are this server describing what it is serving, because a valid
signature does not show that the feed stopped moving an hour ago. 404 names the
markets this oracle does sign.

### `GET /v1/attestation/{market}/at/{ts}`

The attestation signed for that market at exactly that second — the auditor's
question, and the one to ask when checking a liquidation. The market takes `_`
for the slash here too: `/v1/attestation/GOLD_USDX/at/1799999940`. `ts` is the
timestamp inside the attestation the spend was built on. Rotated log files are searched
too, because an attestation old enough to justify a disputed liquidation is
exactly one old enough to have been rotated away. 400 if `ts` is not a number,
404 if there is no attestation at that second.

### `GET /v1/log`

Recent attestations, newest last.

| parameter | default | meaning |
|---|---|---|
| `market` | every market | filter |
| `n` | 50, capped at 1000 | how many |
| `since`, `until` | — | unix seconds; switches to the on-disk archive |
| `cursor` | — | the `cursor` from the previous page |

Without `since`, `until` or `cursor` the answer comes from the in-memory tail
(the last thousand attestations) and costs nothing. With any of them the current
log file is bisected by timestamp and the answer carries `cursor`, a byte offset
to pass back for the next page, or `null` at the end.

`{"attestations": [ … ]}` from memory; `{"attestations": [ … ], "cursor":
65536}` from the archive, with `cursor` `null` on the last page. 400 if any of
the four is not a
number, or `n` is under 1.

### `GET /v1/log/raw`

The log file itself, as `application/x-ndjson`, streamed. `?file=` names one of
the rotated files; without it, the current one. A name that is not one of this
log's own files is a 404 listing the ones that are — a request cannot name a
path of its own.

### `GET /v1/digest`

```json
{"digest": "…", "chained_from": "…", "file": "attestations.log",
 "files": [{"file": "attestations.log.1799000000", "digest": "…",
            "bytes": 268000000, "current": false},
           {"file": "attestations.log", "digest": "…", "bytes": 4200000,
            "current": true, "chained_from": "…"}]}
```

`digest` is the running SHA-256 over everything written to the current file,
seeded with the digest the previous file closed at. So the chain of `.sha256`
files pins every attestation this key has ever signed: publish `digest`
somewhere durable and a rewritten or truncated log stops matching it.

### `GET /v1/seizures`, `GET /v1/seizure/{sighash}`

Every native-BTC seizure this oracle has co-signed, each with the attestation
that justified it:

```json
{"seizures": [
  {"sighash": "…", "signature": "…", "market": "BTC/USDX",
   "strike": 18000000, "attestation": { … }, "ts": 1799999999, "oracle_x": "…",
   "loan": { … }, "offer_sig": "…", "offer_lots": 1}
]}
```

`GET /v1/seizure/{sighash}` returns one such record on its own.

Tier B collateral sits under a plain 2-of-2, so the oracle's signature *is* the
liquidation decision — there is no covenant to refuse it. This log is the whole
of that tier's accountability: anyone can check afterwards that the price behind
a seizure was really under the strike. 404 from `/v1/seizure/{sighash}` if this
oracle co-signed no such seizure.

### `GET /healthz`

```json
{"ok": true, "markets": 6, "signed": 6, "errors": {}, "stale": [],
 "round_error": null, "source_error": null, "clock_skew": 0.3,
 "last_round": 1799999940, "last_signed": 1799999940, "age": 12.4}
```

`ok` means *this oracle is signing*, not *this process is running*: a dead
signing thread, an unwritable log or a feed that stopped answering all leave the
process happily serving its last attestation. A round that has not completed
within two intervals, or within thirty seconds where that is longer, is not ok, and the reply
carries **503** so a check that only reads the status code sees it. A green
oracle answers 200. `signed` counts the markets neither erroring nor stale
and `last_signed` is the newest attestation's timestamp, which is its
observation time;
`clock_skew` is the seconds this host's clock differs from the price source's,
or `null` when the source does not say.

## Verifying a liquidation

Everything above exists so that this is possible without asking anybody:

1. Read the spend that closed the loan and take the `price` and `timestamp` out
   of its witness.
2. Find **the oracle the loan names**, which is the key baked into its vault
   and not necessarily the book's primary — a threshold loan is closed by
   whichever of its oracles signed. `GET /v1/oracles` gives every key this book
   quotes with the public address of each, so the paths below are that
   oracle's. On the testnet the primary is at `/pignus-oracle/` and the others
   at `/pignus-oracle-2/` and `/pignus-oracle-3/`.
3. `GET <that oracle>/v1/attestation/{market}/at/{timestamp}` for the exact
   signed bytes.
4. Check the BIP340 signature against the oracle key **the vault bakes in** —
   not the one `/v1/oracle` or `/v1/pubkey` hands you — and confirm the
   attestation's `price_scale` is the loan's:
   `pignus-cli check-attestation --attestation att.json --oracle-x <key>
   --price-scale <the loan's>` -- without `--price-scale` the scale is printed
   but not compared.
5. `GET <that oracle>/v1/log/raw` for the file that attestation is in, hash it,
   and compare with the `.sha256` beside it and the chain in `/v1/digest`. A log
   that was rewritten to add or remove an attestation stops matching a digest
   published before the rewrite.

For a Tier B seizure the same walk starts at `/v1/seizures`, which carries the
attestation beside the co-signature.
