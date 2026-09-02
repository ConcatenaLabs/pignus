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
on the testnet box) and is published under `/pignus-oracle/`.

**Content type.** Requests and responses are JSON. A POST body that is not an
object, or is not valid JSON, is a 400.

**Numbers.** Amounts in asset atoms are **decimal strings**, because an atom
count can exceed what a JSON number holds exactly in a browser. Heights,
locktimes, timestamps, confirmations, counts, prices, `price_scale` and
`expiry_locktime` are JSON numbers. Two exceptions worth knowing:
`seizure_if_liquidated` and `surplus_if_liquidated` on a loan are numbers, and
`live_debt_by_asset` in `/v1/stats` is keyed by asset id with number values.

**Prices.** A price is debt-asset atoms per collateral-asset atom, multiplied by
`price_scale`. `unit_price` in `/v1/markets` is that number divided out, as a
float, for display only — never compute against it.

**Errors.** Every failure is `{"error": "a sentence"}` with the status code
below. The sentence is written to be shown to a person, so it says what was
wrong rather than naming a Python class.

| code | means |
|---|---|
| 200 | done |
| 400 | the request is malformed, or the chain contradicts it |
| 403 | the request is not signed or tokenised by the party entitled to make it |
| 404 | no such endpoint, offer, loan, take or attestation |
| 409 | that coin is already listed, or that take is already answered |
| 429 | too many writes from this client, or the book is full |
| 500 | a bug; the message names the exception |
| 503 | no node is configured, so a chain lookup cannot be answered |

**Writes are rate limited.** POST and DELETE are charged to a client at one
request a second with a burst of twenty, and to everybody together at twenty a
second with a burst of two hundred. Over either, the answer is 429. Reads are
not limited.

**Behind a proxy.** Every request then arrives from loopback, so `pignusd`
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
  "reference_ticker": "USDX",
  "block_seconds": 60,
  "min_depth": 2,
  "max_price_age": 600
}
```

`lendable` is false when either ticker is unknown to the registry, when the
price is older than `max_price_age`, or when the precisions disagree; `stale` is
true when a price is held but is past that age. `cross_chain` marks a market
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
  "feerate_rfa_per_kvb": 2000,
  "vsize": {"repay": 2000, "repay4": 600, "take": 3000, "…": 0}
}
```

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

### `GET /v1/oracles`

`{"oracles": ["…x-only hex…"], "urls": ["http://…"]}` — every independent oracle
this book quotes against, in configured order, primary first. A lender picks an
m-of-n subset from here.

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

`oracle_x`, `age` (seconds) and `stale` (older than `max_price_age`) are added
beside them. Age is not a signed property and cannot be, but a reader acting on
a price needs it: a signature stays valid however old the number under it is.
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
 "offers": 7,
 "live_debt_by_asset": {"<asset id>": 150000000000},
 "at_risk": [{"loan_id": "…", "market": "GOLD/USDX", "health": 1.02}]}
```

`at_risk` lists LIVE loans whose health is under 1.15, weakest first. A loan in
a market with no fresh price is left out rather than shown at a health of zero,
which would read as "about to be liquidated".

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
- 503 if this book has no node.

### `GET /v1/spend/{txid}/{vout}`

Who spent an outpoint, and what their witness published. This is how a borrower
recovers the secret that releases their Bitcoin collateral without depending on
anybody telling them: a lender who claims a repayment publishes the preimage
whether they mean to or not, because the covenant leaf forces it into the
witness.

```json
{"txid": "…", "vout": 0, "spend_txid": "…",
 "confirmations": 12,
 "preimages": {"<sha256 of the item>": "<32-byte item, hex>"}}
```

`preimages` is every 32-byte witness item keyed by its SHA-256, so a caller
picks by the hash its own loan commits to and this book needs to know nothing
about that loan. The mempool is searched first, then blocks backwards from the
tip as far as `back_scan_cap`.

- 400 if the txid is not 64 hex or the vout is not a number.
- 404 if the output is still unspent, or its spend is outside the scan window.
- 503 if this book has no node.

### `GET /healthz`

```json
{"ok": false,
 "error": "stale price: SILVR/USDX",
 "version": "0.2.0", "git_rev": "0aa3fbb1",
 "covenant_vectors": 13,
 "height": 118432, "last_poll": 1799999950,
 "markets": 6, "priced": 5, "stale_markets": ["SILVR/USDX"],
 "max_price_age": 600, "min_depth": 2,
 "rescan_depth": 1500, "back_scan_cap": 200,
 "offers": 7, "loans": 52, "unrenderable": 0,
 "assets": 41, "fee_rates": 6,
 "block_seconds": 60, "reference_ticker": "USDX",
 "oracles": 3, "oracle_errors": [],
 "event_errors": [], "event_backlog": 0,
 "node": true, "btc_node": true, "btc_height": 155377}
```

`ok` is false when there is no node, when the node or the **primary** oracle is
unreachable, while the first sync is still running, when the poll thread has not
finished within `max(120s, 3 × poll)`, when any market's newest verified
attestation is older than `max_price_age`, or when offer events are queued up
unapplied. `error` says which; `stale_markets`, `oracle_errors` and
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
| `status` | `open` | `open`, `taken`, `withdrawn`, `gone`, `ghost`, or `all` |
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
  "terms": { … the loan terms document … },
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

Drop a listing. The coin is untouched — the coin is the truth, and the lender
withdraws it with `pignus-cli offer-withdraw`.

The manage token goes in the `X-Manage-Token` header, or as `?token=` if a
header is impossible. The query form is redacted from the log; the header is the
right place for it.

`{"removed": true}` on success. 404 if there is no such listing, 403 without the
token, 429 if this client is writing too fast.

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
  "terms": { … },
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
  "height": 118432, "past_maturity": false, "recover_open": false,
  "price": 300000000, "health": 1.6667, "ltv": 0.525,
  "liquidatable": false,
  "seizure_if_liquidated": 3675000, "surplus_if_liquidated": 6662991666,
  "spent_by": "", "spent_height": 0, "closed_confirmations": 0, "note": "",
  "min_depth": 2,
 "funding_height": 118289, "funding_block": "…"
}
```

`funding_height` and `funding_block` are how a Bitcoin-driven reorg is told from
a spend the watcher could not reach, and they are persisted with the record so a
restart does not lose the distinction. `closed_confirmations` is how deep the
CLOSE is, which is a different question from how deep the funding was: a
repayment or a liquidation one block old can still be reorged out, and zero
means either not closed at all or closed only in the mempool. `min_depth` is the
number both are counted towards, repeated on the loan so a reader is not made to
fetch `/v1/markets` to learn what it is.

`single_leaf` says which vault layout this loan lives in: a loan originated
through a funded offer is in the single-leaf vault, a directly originated one is
in the four-leaf tree, and the two have different addresses and different
witnesses. `vault_address` is the scriptPubKey the terms compile to, which is
the thing to compare against the coin before signing anything.

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
 "maturity": 119000, "debt": 10500000000, "collateral": 6666666666,
 "attestations": [{"oracle_x": "…", "price": 17500000,
                   "timestamp": 1799999940, "signature": "…",
                   "present": true, "verified": true}],
 "price_used": 17500000,
 "seize_expected": 3675000, "seize_paid": 3675000,
 "surplus_expected": 6662991666, "surplus_paid": 6662991666,
 "lender_paid": 10500000000,
 "problems": []}
```

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

`lots_left` on an offer is what a take holds against it. A take still `pending`
after half an hour releases its lot, and a `signed` one whose collateral never
appeared releases its lot after six hours: a borrower who asks and walks away
must not hold a lender's offer shut. Every status past that holds its lot for
good, because money is in flight by then.

### `GET /v1/btc/offers`

`?status=` (`open` by default, or `withdrawn`, or `all`). Each row is the stored
offer plus `lots_left`, computed live:

```json
{"offers": [{
  "btc_offer_id": "…24 hex…",
  "loan": {"btc_amount": 100000, "lender_x": "…", "oracle_x": "…",
           "recover_after": 900000, "debt_asset": "…", "debt": 5250000000,
           "principal": 5000000000, "repay_deadline": 125000,
           "abort_after": 902000, "upgrade_fee": 3000, "d_refund": 124000,
           "lender_prog": "…", "lender_ver": 0,
           "market": "BTC/USDX", "strike": 0, "price_scale": 100000},
  "market": "BTC/USDX", "lots": 3, "lots_taken": 1, "lots_left": 2,
  "offer_sig": "…128 hex…", "responder": "", "note": "",
  "status": "open", "created": 1799990000}]}
```

### `GET /v1/btc/offer/{id}`

One offer, with `lots_left`. 404 if there is none.

### `POST /v1/btc/offers`

A lender publishes an offer. Body: `loan` (the fields above; `btc_amount`,
`lender_x`, `oracle_x`, `recover_after`, `debt_asset`, `debt`, `repay_deadline`,
`abort_after`, `d_refund` and `lender_prog` are all required and must be
non-empty), `market`, `lots`, `offer_sig`, and optionally `responder` and a
`note` of up to 200 characters.

`offer_sig` is the lender's BIP340 signature over the offer's own terms. It is
what makes this endpoint safe to leave open: an offer carrying somebody else's
key would make **their** responder pay it out.

The id is derived from the signed terms, so republishing the same offer is
idempotent — it keeps the record's age and what has already been taken — and two
different offers can never collide.

- 400 if a required field is missing or a payout program's length does not match
  its witness version (20 bytes at v0, 32 at v1).
- 403 if the signature is not by the key the offer names as the lender.

### `POST /v1/btc/offers/{id}/withdraw`

Body: `{"sig": "…"}`, the lender's signature over the withdrawal. Sets the
offer's status to `withdrawn`. 404 for an unknown offer, 403 if the signature is
not the publishing lender's.

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
 "btc_height": 150773,
 "reclaim_dest": "0014…", "reclaim_fee": 3000}
```

The relay rebuilds the loan from the offer's own terms plus the four things a
taker chooses (`borrower_x`, `h_w`, `borrower_prog`, `borrower_ver`) and
refuses the request unless the pre-vault address and value it derives match the
outpoint named. It also refuses an outpoint another take already names: one coin
funds one loan.

`200` returns the take with `status: "requested"`, its derived `prevault_spk`
and `disbursement_spk`, and any `warnings` its deadlines raise. `404` no such
offer. `409` the offer is closed, or every lot is spoken for. `400` anything the
relay could not rebuild.

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
is not the lender's. `409` the take already has a different hash. `400` the hash
is not 32 bytes.

### `POST /v1/btc/presig`

The borrower signs the one transaction that can move their collateral into the
vault -- which they can only derive now that the hash exists.

```json
{"take_id": "…", "upgrade_presig": "<64-byte hex>"}
```

The relay verifies the signature against the loan it rebuilt, so a lender is
never asked to fund a loan that could not start. `200` moves the take to
`status: "pending"`. `409` the take has no hash yet. `400` the signature does
not move that collateral into that vault.

### `GET /v1/btc/takes`

`?status=`, `?offer_id=`, `?borrower_x=`. `{"takes": [ … ]}`, newest first.
Asking by borrower key is what lets somebody who cleared their browser storage,
or moved to another machine, find their own loans again.

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
accepted on the way in. The second name is what the wire has always called it,
from when this really was an adaptor signature; what it carries now is a plain
release, and the older name is kept only so a client and a relay of different
vintages still understand each other.

`auth` is a BIP340 signature by the offer's `lender_x` over
`tagged("pignus/btc-adaptor/1", canonical({take_id, adaptor_point,
payment_hash, adaptor_sig}))`.

The relay derives the vault from the take's own pre-vault outpoint and payment
hash, rebuilds the reclaim transaction, and refuses a signature that does not
verify against it. `200` moves the take to `status: "signed"` and serves the
`vault_txid` it derived. `403` the reply is not the lender's. `409` the take has
no advance signature yet, the release is for a different payment hash, or one is
already stored. `400` the signature does not verify.

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

### `POST /v1/btc/claimed-principal`, `/v1/btc/repaid`

The borrower's own reports: where they took the principal, and where they paid
the debt. Both are hints -- everything they say is on chain -- and both record
`txid`/`vout` and change NO status, because a status a borrower can set is one
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

`200` on either. `403` the signature is not the borrower's. `404` no such take.

# `pignus-oracle`

Everything the oracle serves is a read. The ones that go to disk for it —
`/v1/log/raw`, `/v1/seizures`, `/v1/seizure/{sighash}`,
`/v1/attestation/{market}/at/{ts}`, and `/v1/log` whenever `since`, `until` or
`cursor` sends it to the archive — are limited to one request a second per
address with a burst of ten, and answer 429 over that. An auditor's query is
cheap once and expensive in a loop, and this process has signing to do.
Everything else comes out of memory and is not limited.

## Endpoints

### `GET /v1/pubkey`

`{"oracle_x": "…64 hex…", "price_scale": 100000, "previous": ["…"]}` — the key
vaults bake in. `previous` lists keys this oracle used to sign with, so a
borrower's page can tell a rotation from a stranger. Live vaults bake the key
they were originated against, so a rotation is never a swap.

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

`{"attestations": [ … ], "cursor": 65536}`. 400 if any of the four is not a
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
{"sighash": "…", "signature": "…", "market": "BTC/USDX",
 "strike": 18000000, "attestation": { … }, "ts": 1799999999, "oracle_x": "…"}
```

Tier B collateral sits under a plain 2-of-2, so the oracle's signature *is* the
liquidation decision — there is no covenant to refuse it. This log is the whole
of that tier's accountability: anyone can check afterwards that the price behind
a seizure was really under the strike. 404 from `/v1/seizure/{sighash}` if this
oracle co-signed no such seizure.

### `GET /healthz`

```json
{"ok": true, "markets": 6, "errors": {}, "stale": [],
 "round_error": null, "source_error": null,
 "last_round": 1799999940, "age": 12.4}
```

`ok` means *this oracle is signing*, not *this process is running*: a dead
signing thread, an unwritable log or a feed that stopped answering all leave the
process happily serving its last attestation. A round that has not completed
within two intervals (never less than thirty seconds) is not ok, and the reply
carries **503** so a check that only reads the status code sees it. A green
oracle answers 200.

## Verifying a liquidation

Everything above exists so that this is possible without asking anybody:

1. Read the spend that closed the loan and take the `price` and `timestamp` out
   of its witness.
2. `GET /pignus-oracle/v1/attestation/{market}/at/{timestamp}` for the exact
   signed bytes.
3. Check the BIP340 signature against the oracle key **the vault bakes in** —
   not the one `/v1/oracle` or `/v1/pubkey` hands you — and confirm the
   attestation's `price_scale` is the loan's:
   `pignus-cli check-attestation --attestation att.json --oracle-x <key>`.
4. `GET /pignus-oracle/v1/log/raw` for the file that attestation is in, hash it,
   and compare with the `.sha256` beside it and the chain in `/v1/digest`. A log
   that was rewritten to add or remove an attestation stops matching a digest
   published before the rewrite.

For a Tier B seizure the same walk starts at `/v1/seizures`, which carries the
attestation beside the co-signature.
