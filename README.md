# Pignus

Non-custodial collateralised lending on Sequentia. Borrow one issued asset
against another -- or against native Bitcoin on the parent chain -- with the
loan's terms compiled into a covenant and enforced by the script interpreter
rather than by an operator.

Design and security analysis: [`docs/pignus-design.md`](docs/pignus-design.md).

## What it actually guarantees

A borrower locks collateral in one taproot UTXO with a NUMS internal key -- so
there is no key path -- and four leaves that are the only ways out:

| leaf | who | needs | does |
|---|---|---|---|
| `REPAY` | anyone | nothing at all | pay the lender the debt, return the whole collateral to the borrower |
| `LIQUIDATE` | anyone | an oracle attestation under the strike | pay the lender, keep the bonus, return the surplus |
| `DEFAULT` | anyone | an attestation, after maturity | the same seizure, at any price |
| `RECOVER` | anyone | a long timeout after maturity | sweep the whole collateral to the lender's pinned address: the oracle-liveness backstop |

No exit needs a signature. Every leaf reads what it enforces out of the
transaction and pays a destination baked into the address, which is what lets a
browser wallet drive all four -- a wallet can sign its own inputs, but not a
covenant leaf.

Every term -- both asset ids, the total repayment, both payout scriptPubKeys,
the oracle key, the price feed, the strike, the maturity, the bonus -- is a
constant inside those leaves, and the leaves are committed inside the taproot
output key. So the terms and the address are the same fact stated twice, and
that is what makes the one check below sufficient.

`REPAY` goes further: no oracle either, and no witness data at all. A solvent
borrower can always leave, whatever anyone else does.

## The one check

```
pignus-cli verify --terms loan.json --txid <funding> --vout 0
```

It rebuilds the vault address from the terms you agreed and compares it to the
output actually being funded, and it asserts the internal key is NUMS. A loan
whose debt is one atom different, or whose lender, oracle, market or strike has
been swapped, compiles to a different address and is refused.

`--txid` reads the output out of the utxo set with `gettxout`, so no transaction
index is needed. A vault that has already been spent is not in that set: check
one with `--blockhash <block>`, or with `--spk <hex>` if you have the
scriptPubKey directly. There are two vault layouts -- the four-leaf tree of a
directly originated loan and the single-leaf vault a funded offer creates -- and
the command accepts either and reports which one matched, or insists on one with
`--four-leaf` / `--single-leaf`.

Run this before signing anything, and again on the vault a take produced:
`offer-take` prints a record `--terms` reads, so the check is the same one
whether the loan came from a resting offer or was originated directly.
Everything Pignus claims reduces to it; a wallet or a book that skips it has
quietly reintroduced a trusted party.

## Layout

```
pignus/compat.py         imports the PROVEN covenant and refuses a drifted one
pignus/terms.py          LoanTerms: the agreement, the address, and verify_funding()
pignus/oracle.py         attestation format, signing, verification, price quoting
pignus/vault.py          every transaction: fund/take/withdraw an offer, the four
                         exits, explicit-coin preparation for node wallets
pignus/fees.py           a fee in any asset, from the node's exchange rates
pignus/watcher.py        reconcile loans AND offers to the chain; name each exit;
                         discover loans from take witnesses; catch ghosts
pignus/book.py           the loan book: discovery, nothing else
pignus/offers.py         funded resting offers (the node repo's pignus_offer.py)
pignus/btc_collateral.py native BTC collateral (Tier B): the pre-vault, the
                         vault, and both chains' legs of a cross-chain loan
pignus/btc_relay.py      what a relay may and may not be believed about: the
                         signatures on every offer and every lender's report
pignus/adaptor.py        Schnorr adaptor signatures, the cross-chain link
pignus/dlc.py            DLC primitives for settling BTC collateral at maturity;
                         a library, wired into nothing yet
pignus/btcscript.py      the Bitcoin script and taproot primitives Tier B needs
pignus/openamp.py        Tier C pledges at an OpenAMP policy server
pignus/repurchase.py     Tier D: the OpenDAMP repurchase, labelled as one, never a loan
pignus/node.py           a thin JSON-RPC client
bin/pignus-oracle        sign prices on a timer and publish them
bin/pignusd              the loan book, the watcher, the cross-chain relay, and
                         the page at /lending/
bin/pignus-cli           selftest, quote, propose, show, address, verify, status,
                         explain, check-attestation; with a node wallet:
                         offer-fund, offer-publish, offer-take, offer-withdraw,
                         loan-export, repay, liquidate, default, recover; btc-*
                         (Tier B, both chains); pledge-* (Tier C); repo-*
                         (repurchase)
bin/pignus-liquidator    one liquidator among however many people run one
web/                     the browser client pignusd serves: pignus.js, offer.js,
                         repurchase.js, pset.js, flows.js, wallet.js, app.js,
                         and for Tier B btc.js, adaptor.js, btcborrow.js
deploy/                  systemd units (oracle, book, cross-chain responder),
                         example configs, DEPLOY.md
docs/pignus-design.md    the design and security analysis
docs/api.md              every HTTP endpoint the book and the oracle serve
```

There is one **proven** implementation of the covenant, in the node
repository's `test/functional/pignus_covenant.py`, proven against a node by
`feature_pignus_vault.py`. This package imports it rather than porting it: a
port that differs by a single byte derives a different address, and the failure
mode of a wrong vault address is collateral nobody can ever spend.
`pignus/vectors.json` exists for implementations that genuinely cannot import
Python. The browser is that second implementation -- `web/pignus.js` and
`web/offer.js` for the covenant, `web/repurchase.js` for the repurchase vault,
`web/btc.js` and `web/adaptor.js` for the parent chain -- each pinned byte for
byte to vectors the proven Python emits (`pignus/vectors.json`,
`web/btc_vectors.json`, `web/adaptor_vectors.json`), and the page refuses to run
if the loan pinning fails. `compat.verify_builder()` uses the vectors here as a
tripwire, refusing to derive addresses from a builder that has changed; it runs
the first time any process loads the covenant, so a drifted checkout is caught
before an address is derived from it rather than after.

The book follows the chain on its own. An offer's coin is watched; when it is
taken, the borrower's payout program is read out of the take witness, the new
vault is registered as a loan, and the offer moves to its remainder. A loan
taken by any wallet, through the page or not, turns up on the page, provided the
take is within the watcher's scan depth (`rescan_depth` blocks); after a longer
outage, start the daemon once with `--rescan-from <height>`.

## From the command line

Everything the page does, from a node wallet. Each command derives the address
it acts on from the terms and checks it against the coin before building
anything; fees are priced from the node's exchange rates in whatever the wallet
holds; and coins are prepared explicit when the wallet only has blinded change,
which a covenant cannot spend.

```
pignus-cli offer-fund --market GOLD/USDX --principal 100 --lots 3 \
    --interest 3 --open-ltv 50 --liq-ltv 75 --term-days 30 --rpc-wallet me
pignus-cli offer-take --offer <id> --rpc-wallet me
pignus-cli repay | liquidate | default | recover --loan <id> --rpc-wallet me
pignus-cli offer-withdraw --offer <id> --rpc-wallet me
```

`--book` names the pignusd to read markets and offers from (default
`http://127.0.0.1:8741`, or `PIGNUS_BOOK`). The node wallet that signs is named
by `--rpc` (default `http://127.0.0.1:18776`, the RPC port of the binary's
default chain; `PIGNUS_RPC_URL`), `--rpc-wallet`, and either `--rpc-cookie` or
`--rpc-user`/`--rpc-password`. Every one of them also reads a `PIGNUS_RPC_*`
environment variable, which is where credentials belong: a command line is
readable by every process on the machine.

A loan does not need the book at all. `--loan <id>` is a convenience that looks
the terms up; with the terms file in hand, `pignus-cli repay --terms loan.json
--txid <vault txid> --rpc-wallet me` closes it against nothing but a node.
`pignus-cli loan-export` writes that file out of the book for keeping.

After a loan closes, `pignus-cli explain --loan <id>` reads the ending back off
the chain: which exit was taken, the attested price behind a seizure, checked
against the key the vault itself bakes in, and what the transaction actually
paid each party against what the terms say that price buys. It works from
`--terms` and `--txid` too, with no book involved.

### Threshold oracles

An m-of-n loan bakes in several independent oracles and needs `threshold` of
them to agree before it can be liquidated:

```
pignus-cli offer-fund --market GOLD/USDX --principal 100 \
    --oracles book --oracle-threshold 2 --rpc-wallet me
```

`--oracles book` uses every oracle the book quotes against; the CLI, the
liquidator and the browser all assemble the threshold witness.

### Running a liquidator

Liquidation is a permissionless race. Every racer must pay the lender in full
and return the borrower's surplus, so the borrower and the lender are
indifferent to who wins, and the bonus is what prices the race. Nothing about
running one is privileged, and nothing on the testnet runs one for you.

```
pignus-liquidator --book http://127.0.0.1:8741 --rpc http://127.0.0.1:18776 \
    --rpc-wallet liquidator --taker-address <addr> --dry-run --once
```

**Always start with `--dry-run --once`**: it reports what it would do and
touches nothing.

| flag | what it does |
|---|---|
| `--book` | a pignusd to read LIVE loans from, re-read every round, so a loan that appears while it runs is watched too |
| `--oracle URL` | an oracle to read prices from, repeatable; needed when there is no book, and worth adding for loans baked to a key the book does not quote |
| `--loans FILE` | a JSON list of `{"terms": …, "txid": …, "vout": 0, "single_leaf": bool}` instead of a book |
| `--taker-address` / `--taker-spk` | where seized collateral is paid |
| `--fee-asset` / `--fee-amount` | the fee asset and atoms; by default the debt asset being spent, else anything held with a published rate |
| `--min-profit ATOMS` | skip a seizure worth less than this much more than the debt it pays (default 0: never at a loss) |
| `--max-attestation-age S` | ignore a price signed longer ago (default 600); the covenant cannot check recency, so this is the only place it is checked |
| `--allow-stale` | act on older prices anyway |
| `--call-due` | also call loans past maturity, through DEFAULT |
| `--interval` / `--once` | how often a round runs, or run one and stop |

At least one of `--book` and `--oracle` is required. The RPC flags take the same
`PIGNUS_RPC_*` environment defaults as `pignus-cli`, so credentials need not be
on the command line.

Every attestation is verified here against the key **that loan** names and at
the scale **that loan** computes with, not against whichever oracle served it: a
price signed by another key, or quoted at another scale, is a number about a
different loan.

The wallet must hold the **debt** asset -- a liquidator pays the lender in full
out of its own pocket and keeps collateral worth more -- plus enough of some
asset with a published exchange rate to pay the fee.

### Native BTC collateral (Tier B, cross-chain)

Borrow a Sequentia asset against real Bitcoin. The collateral sits on Bitcoin,
the debt on Sequentia, bound by an adaptor signature so repaying and reclaiming
are one act.

Origination is atomic, because otherwise it is a gift: the collateral waits in a
pre-vault the borrower can take back, the principal is paid into an output only
the borrower can open, and opening it publishes the secret that moves the
collateral into the vault. Neither side ever holds both, and the only party
exposed to a loss rather than a delay is a lender who goes offline in the middle
of it. `pignus-cli btc-check` prints where a loan stands and what each party can
do next.

A borrower does all of this in the browser at `/lending/`. From the command
line, it is a two-party handshake, one command per move, with a `ticket` JSON
passed between the parties (public state only, never a key or a secret):

```
pignus-cli btc-keygen --out lender.key            # each party once
pignus-cli btc-propose --lender-key lender.key --oracle-x <x> \
    --btc-amount 100000 --debt-asset <id> --debt 5250000000 \
    --principal 5000000000 --lender-prog <hex> --market BTC/USDX \
    --recover-after <btc-height> --abort-after <btc-height> \
    --repay-deadline <seq-height> --d-refund <seq-height> --out loan.json
pignus-cli btc-prepare  loan.json --btc-rpc ...   # borrower: fund unbroadcast
pignus-cli btc-adaptor  loan.json --lender-key lender.key
pignus-cli btc-originate loan.json --btc-rpc ...  # borrower: verify, then fund
pignus-cli btc-repay    loan.json --rpc ...       # borrower: pay the hashlock
pignus-cli btc-claim    loan.json --lender-key lender.key --rpc ...   # reveals t
pignus-cli btc-reclaim  loan.json --borrower-key borrower.key --rpc ... --btc-rpc ...
```

and the other endings: `btc-seize-sighash` / `btc-seize` (lender + oracle),
`btc-timeout` (lender, after the term), `btc-refund` (borrower, if the lender
stalls), `btc-abort` (borrower, if the principal never came). The trust model,
the exposure at each step and why liquidation needs the oracle to co-sign on the
Bitcoin side are in the design doc, section 7.

A seizure is the one move that needs a third party while a loan is live: there
is no covenant on the Bitcoin side, so the oracle's signature *is* the decision.
`btc-seize-sighash --out` writes a request carrying the loan, the oracle
operator co-signs it with `pignus-oracle --sign-seize --request`, and both the
signature and the attestation behind it are published at the oracle's
`/v1/seizures` -- so a seizure that was not justified is visible to anyone
afterwards, which is the whole of the accountability this tier has.
`deploy/DEPLOY.md` has the procedure.

#### Through the book

Passing a ticket by hand is fine for two people who already know each other. To
be found by strangers, a lender publishes an offer on a `pignusd` and keeps a
responder running against it:

```
pignus-cli btc-offer-publish --config responder.json --market BTC/USDX \
    --oracle-x <x> --strike <price> --btc-amount 100000 \
    --debt-asset <id> --debt 5250000000 --principal 5000000000 \
    --lender-prog <hex> --lots 3 \
    --recover-after <btc-height> --abort-after <btc-height> \
    --repay-deadline <seq-height> --d-refund <seq-height>
pignus-cli btc-respond --config responder.json --watch
```

The responder signs releases, pays principals once the collateral is confirmed,
starts loans as borrowers claim them, and takes back what nobody claimed. Its
configuration file carries the node credentials and the path to the lender's
key, so nothing secret is on the command line, where every user on the machine
can read it. `deploy/responder.example.json` is the starting point and
`deploy/pignus-btc-responder.service` runs it. It also keeps a state file, which
is what stops a principal being paid twice after a crash: back it up with the
key.

Every offer is signed by the key it names as the lender, and the relay verifies
that before storing it -- otherwise anyone could publish in a lender's name and
have that lender's own responder pay it out. The same goes the other way: every
report a responder makes about a take is signed, and a borrower's page checks it.

A borrower takes an offer from the page's BTC tab, or from the command line:

```
pignus-cli btc-offer-take --offer <id> --borrower-key borrower.key \
    --borrower-prog <hex> --out loan.json --btc-rpc ... --rpc ...
```

The relay itself is never trusted: it holds no key, moves nothing, and rebuilds
every address, outpoint and sighash from the offer's own terms rather than
believing what it is told. [`docs/api.md`](docs/api.md) documents each endpoint.

## Running it

The package needs a Sequentia **source** checkout, because that is where the
proven covenant lives. It looks for one at `../Sequentia` (beside this
checkout), at `~/Sequentia`, or vendored at `vendor/sequentia`; `SEQUENTIA_SRC`
names one anywhere, and is taken as a decision rather than a hint, so a wrong
one is reported instead of silently falling back.

```
tests/cli_drill.sh        # offline, no node, ~2 seconds
pignus-cli selftest                      # vectors + an oracle round trip
```

`tests/test_btc_collateral.py` also needs a Bitcoin Core `bitcoind`
(`PIGNUS_BITCOIND`, default `~/bitcoin-28.0/bin/bitcoind`), and the
`tests/*.mjs` browser checks need Node.

### The book and the page

`pignusd` serves the loan book, the chain watcher, the cross-chain relay and the
browser client, and on the testnet it is what `/lending/` is.
`deploy/DEPLOY.md` covers running it and the oracle as systemd units behind
Caddy, with `deploy/pignusd.example.json` as the starting configuration, and
[`docs/api.md`](docs/api.md) documents every endpoint it serves.

### The oracle

```
pignus-oracle --config oracle.json
```

```json
{
  "keyfile":       "/var/lib/pignus/oracle.key",
  "logfile":       "/var/lib/pignus/attestations.log",
  "listen":        "127.0.0.1:8730",
  "interval":      60,
  "price_scale":   100000,
  "markets":       ["GOLD/USDX", "SILVR/USDX", "OILX/USDX", "BTC/USDX"],
  "symbols":       {"BTC": "tBTC"},
  "precisions":    {"GOLD": 8, "SILVR": 8, "OILX": 8, "BTC": 8, "USDX": 8},
  "log_max_bytes": 256000000,
  "source":        {"type": "http_bulk", "url": "http://127.0.0.1:8088/prices",
                    "field": "price", "timeout": 8, "max_age": 300,
                    "feed_max_age": 300}
}
```

| key | what it is |
|---|---|
| `markets` | the feeds this oracle signs, `COLLATERAL/DEBT` |
| `precisions` | each named asset's decimal count. **Give every one an entry**: a missing one is assumed to be 8, and where that is wrong the signed price is wrong by a power of ten, which no signature check downstream can catch. A config that names some and not others is refused at start |
| `symbols` | the ticker the feed knows an asset by, where it differs from the market's name |
| `price_scale` | what a price is multiplied by before signing (default `1e5`) |
| `interval` | seconds between signing rounds |
| `listen` | `host:port` |
| `log_max_bytes` | rotate the attestation log past this size (0, the default, never rotates) |
| `previous_keys` | x-only keys this oracle used to sign with, published at `/v1/pubkey` so a borrower can tell a rotation from a stranger |
| `seizures` | where Tier B co-signatures are logged (default: `<logfile>.seizures`) |
| `source.type` | `static` (fixed prices, for drills), `http` (one request per market) or `http_bulk` (one snapshot per round, which is what keeps a round's prices consistent) |
| `source.url`, `.field` | where the prices are, and which field of each row holds one |
| `source.timeout`, `.max_age` | seconds to wait, and how long a fetched snapshot may be reused |
| `source.feed_max_age` | how old the feed's own `_meta.updated` may be before this oracle refuses to re-sign its numbers |

8730 is the built-in listen default; the testnet box runs the oracle on 8740
and `pignusd` on 8741, see `deploy/DEPLOY.md`.

The key is created 0600 on first run and its mode is re-checked on every start.
It is never logged and never served. Prices come from the price feed that
already drives the any-asset fee market (the node repository's
`contrib/price-server`) -- deliberately not a second price pipeline.

Endpoints: `/v1/pubkey`, `/v1/markets`, `/v1/attestation/{market}` (use `_` for
the slash) and `/v1/attestation/{market}/at/{ts}`, `/v1/log`, `/v1/log/raw`,
`/v1/digest`, `/v1/seizures`, `/v1/seizure/{sighash}`, `/healthz`. All of them
are in [`docs/api.md`](docs/api.md).

`/healthz` answers 503, not 200, when the oracle has not completed a signing
round within two intervals: the process staying up while the signing thread is
dead is exactly the outage that otherwise goes unnoticed until it reaches the
`RECOVER` backstop.

Co-signing a Tier B seizure is an operator's command rather than an endpoint,
because it moves someone's bitcoin. It refuses unless this oracle's own last
published price is under the strike, and it publishes the co-signature next to
the attestation that justified it at `/v1/seizures`:

```
pignus-oracle --config oracle.json --sign-seize --request seizure.json
```

`seizure.json` is what `pignus-cli btc-seize-sighash --out` writes: it carries
the loan, so the oracle rebuilds the sighash from the terms rather than signing
a number somebody else computed.

### Verifying a liquidation

Anyone can check one afterwards, with nothing privileged:

1. Read the closing transaction's witness for the price and the timestamp
   (`pignus-cli explain` prints both).
2. Fetch the exact signed bytes:
   `GET /v1/attestation/{market}/at/{timestamp}`.
3. `pignus-cli check-attestation --attestation att.json --oracle-x <the key the
   VAULT bakes in>`, and confirm the attestation's `price_scale` is the loan's.
   A signature by another key, or a price quoted at another scale, is a number
   about a different loan.
4. Download the log file it is in from `/v1/log/raw`, hash it, and compare with
   the `.sha256` beside it and the chain in `/v1/digest`. A log rewritten to add
   or remove an attestation stops matching a digest published before the
   rewrite.

### Prices

A price is **debt-asset atoms per collateral-asset atom, scaled by
`price_scale`** (default `1e5`). Quoting per atom is what keeps the covenant
ignorant of either asset's decimals.

```
pignus-cli quote --market GOLD/USDX --collateral-ref 3000 --debt-ref 1
```

prints `300000000`: 3,000 USDX atoms per GOLD atom once the 1e5 scale is
divided out, i.e. 3,000 USDX per GOLD. Beside it comes `strike_at_liq_ltv`, the
strike a loan opened at `--open-ltv` (50% by default) would need for each of the
usual liquidation ratios -- the same arithmetic `offer-fund` does, so a lender
can see the number before committing to it.

### Size limit

The seizure forms `gross * price_scale` on chain, and `OP_ADD64` aborts on
64-bit overflow, so at 8 decimals and the default scale one loan caps at about
**878,000 units** of the debt asset. Lower `price_scale` to `1e4` or `1e3` for
~8.8M or ~88M, trading price precision for size. The builders assert the bound
at construction, so a loan that could not be liquidated cannot be created.

## What is trusted, and what is not

The oracle can assert a price low enough to open `LIQUIDATE`. That is all of it.
It cannot move funds, choose a recipient, change how much is seized, trigger a
default before maturity, stop a repayment, or keep the borrower's surplus. Its
worst behaviour -- fabricating a dip -- costs a borrower the bonus and the price
difference on the seized portion, is bounded, and is permanently visible in the
published log. Section 6.1 of the design doc has the full accounting, and
section 5 has the one honest gap: nothing in tapscript can prove an attestation
is *recent*, only that it is newer than origination.

The platform is trusted for nothing: the book is discovery, the watcher is
read-only, and the liquidator bot is just whoever noticed first.

Three collateral types are weaker on purpose and are labelled as such:

- **Native BTC** (design doc section 7) is cross-chain. Repayment and release
  are bound by an adaptor signature, so neither can happen without the other --
  but a lender who simply declines to claim the repayment keeps the collateral,
  which on an over-collateralised loan is worth more. Liquidation needs the
  oracle to co-sign, because Bitcoin has no introspection. Section 7.2 explains
  why that is not a DLC, and 7.1's exposure table says exactly what each party
  can lose.
- **OpenAMP restricted assets** never enter a vault at all: the issuer's policy
  server records a pledge and refuses transfers that would spend it. A release
  needs the lender's signature; a seizure needs the lender's signature plus
  either maturity or the borrower's countersignature. The lender's security is
  the issuer's promise rather than a script, so this is *not* non-custodial in
  the sense above -- it is inherent to a transfer-restricted asset, and the CLI
  labels every pledge issuer-permissioned.
- **OpenDAMP assets** cannot be collateral at any price, for three structural
  reasons the design doc sets out. What Pignus offers instead is a *repurchase*:
  the borrower sells the asset to the lender, and the lender's obligation to
  sell it back is secured by a bond in a two-leaf covenant vault (`repo-*`,
  design doc 8.1). It is never shown as a loan, because it is not one -- the
  borrower has sold their asset and holds a claim.

## Tests

`tests/run-tests.sh` runs everything below, fastest first, so a mistake surfaces
in seconds rather than after a two-chain rig has finished starting. Each file's
docstring says what it proves.

Offline, no node:

```
tests/cli_drill.sh                 every CLI command, refusals included
tests/service_drill.sh             the oracle and pignusd together
tests/test_units.py                the covenant vectors + an oracle round trip
tests/test_openamp.py              the Tier C pledge message, pinned
tests/test_btc_relay_auth.py       what the relay may be believed about
tests/test_web.mjs                 web/pignus.js against the golden vectors
tests/test_offer_web.mjs           web/offer.js against them
tests/test_repurchase_web.mjs      web/repurchase.js against them
tests/test_btc_web.mjs             web/btc.js against web/btc_vectors.json
tests/test_adaptor_web.mjs         web/adaptor.js against adaptor_vectors.json
```

Against a running chain (a `sequentiad`, and for the BTC ones a `bitcoind` too):

```
tests/test_platform.py             this library, end to end
tests/test_pset.py                 the browser's PSETs, accepted by a node
tests/test_flows.py                the browser's flows through a whole loan
tests/test_book.py                 the book and the watcher against a chain
tests/test_tiers.py                Tiers C and D
tests/test_lifecycle.py            the CLI through fund, take, repay,
                                   liquidate, withdraw, default, with the
                                   daemon discovering every step
tests/test_threshold.py            a 2-of-3 oracle loan, end to end
tests/test_btc_relay.py            the relay and the lender's responder
tests/test_btc_disburse.py         paying the principal
tests/test_btc_collateral.py       Tier B's covenant and crypto
tests/test_btc_cli.py              the BTC-collateral library legs
tests/test_btc_cli_flow.py         the BTC-collateral CLI handshake
tests/test_prevault.py             origination on the Bitcoin side: the
                                   pre-vault, the upgrade, the abort
tests/test_btc_origination.py      a whole cross-chain loan, both chains,
                                   nobody trusting anybody
```

In the node repository, and in its `test_runner.py`:

```
test/functional/feature_pignus_vault.py      the covenant's exits and refusals
test/functional/feature_pignus_oracle_set.py the on-chain oracle set
test/functional/feature_pignus_offer.py      funded offers
test/functional/feature_pignus_hashlock.py   the signature-free hashlock sweep
                                             both cross-chain legs are paid
                                             through
test/functional/feature_pignus_attack.py     the attack suite
```

`tests/test_platform.py` needs the `test/config.ini` a built checkout's
`configure` writes, and is skipped without one; the rest of the chain tests
start their nodes through `tests/rig.py` and need only the binaries.

`tests/gen_web_vectors.py` regenerates the golden vectors the browser's Bitcoin
and adaptor code pins itself to. Run it only when the Python it mirrors changes,
in the same commit, and re-run `test_btc_web.mjs` and `test_adaptor_web.mjs`.
