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
pignus/adaptor.py        BIP340 signing and verification, and the Schnorr
                         adaptor signatures the DLC settlement uses
pignus/dlc.py            DLC primitives for settling BTC collateral at maturity;
                         a library, used by nothing else here
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

`pignus-cli <command> --help` is the complete list of options for any command.
What shapes an offer is worth having here:

| flag | what it sets |
|---|---|
| `--principal` / `--lots` | lent per loan, and how many loans the one coin holds |
| `--interest` | percent over the whole term (default 3) |
| `--open-ltv` / `--liq-ltv` | the loan-to-value a loan opens at and the one it liquidates at (default 50 and 75) |
| `--term-days` / `--offer-days` | the term, and how long the offer stays open (default: the term) |
| `--bonus` | the liquidation bonus, percent (default 5) |
| `--borrower-ver` | the witness version borrowers are paid out at: 0 for a bech32 extension wallet, 1 for taproot. It is part of the offer's address, so it cannot be changed afterwards |
| `--oracles` / `--oracle-threshold` | an m-of-n oracle set, below |
| `--memo` | a note kept in the terms |
| `--no-publish` | fund the offer without listing it on the book |

`--book` names the pignusd to read markets and offers from (default
`http://127.0.0.1:8741`, or `PIGNUS_BOOK`). The node wallet that signs is named
by `--rpc` (default `http://127.0.0.1:18776`, the RPC port of the binary's
default chain; `PIGNUS_RPC_URL`), `--rpc-wallet`, and either `--rpc-cookie` or
`--rpc-user`/`--rpc-password`. Every one of them also reads a `PIGNUS_RPC_*`
environment variable, which is where credentials belong: a command line is
readable by every process on the machine.

Every command that composes a covenant transaction takes the same three fee
options. `--fee-asset` and `--fee-amount` name the asset and the atoms; the
default is the asset already being spent, and failing that anything held with a
published rate. Nothing falls back to a privileged asset, because there is not
one. `--prep-fee-asset` names the asset for the preparing sends -- the ones that
give the wallet explicit coins, since a covenant cannot spend a blinded input
and a node wallet's change is blinded -- and defaults to `--fee-asset`. The
cross-chain `btc-*` commands take `--fee-asset` on its own, their Sequentia legs
being one plain payment each. `--dry-run` composes and prints without
broadcasting anything, preparing sends included.

`liquidate` and `default` take their price from the book, or from
`--attestation`, `--attestations` (for an m-of-n loan) or `--oracle`. It is
verified locally against the key the **vault** bakes in either way, and refused
if it was signed more than `--max-attestation-age` seconds ago -- 600 by
default, `--allow-stale` to build against an older one anyway. Tapscript can
tell that an attestation is newer than the loan but not that it is recent, so
this is the only place recency is checked at all.

`pignus-cli --version` prints the version and `--debug` re-raises with the
traceback instead of a one-line message. A command exits 1 for an error, 2 when
an issuer refused a Tier C request outright, and 3 when the covenant builder
could not be loaded or has drifted from the golden vectors.

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
the debt on Sequentia, and **one hash appears in both chains' scripts**: the
Bitcoin vault's `RECLAIM` leaf demands the secret behind it alongside both
parties' signatures, and the Sequentia repayment can only be taken by publishing
that same secret. So repaying and reclaiming are one act, and the borrower can
check that for themselves rather than being asked to believe it -- the hash they
repay against is the hash their release is built on.

Origination is atomic, because otherwise it is a gift: the collateral waits in a
pre-vault the borrower can take back, the principal is paid into an output only
the borrower can open, and opening it publishes the secret that moves the
collateral into the vault. Neither side ever holds both, and the only party
exposed to a loss rather than a delay is a lender who goes offline in the middle
of it. `pignus-cli btc-check` prints where a loan stands on both chains and
whose move it is next.

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
pignus-cli btc-prepare  loan.json --borrower-key borrower.key \
    --borrower-prog <hex> --btc-rpc ...   # fund the pre-vault, unbroadcast
pignus-cli btc-adaptor  loan.json --lender-key lender.key   # draw the secret,
                                                            # publish its hash,
                                                            # sign the release
pignus-cli btc-originate loan.json --borrower-key borrower.key --btc-rpc ...
pignus-cli btc-disburse loan.json --rpc ...       # lender: pay the principal
pignus-cli btc-claim-principal loan.json --borrower-key borrower.key --rpc ...
pignus-cli btc-upgrade  loan.json --lender-key lender.key --btc-rpc ...
pignus-cli btc-repay    loan.json --rpc ...       # borrower: pay the hashlock
pignus-cli btc-claim    loan.json --lender-key lender.key --rpc ...   # reveals t
pignus-cli btc-reclaim  loan.json --borrower-key borrower.key --rpc ... --btc-rpc ...
```

The order matters and is not a matter of taste. The vault's address commits to
the lender's hash, so the borrower cannot sign the move into it until the lender
has drawn the secret; and the lender cannot start the loan until the borrower
has opened the principal, which is what publishes the secret `btc-upgrade`
needs. Every step before `btc-originate` commits nothing at all. `btc-check`
names the next move at each stage, so the sequence need not be memorised.

`--borrower-prog` and `--borrower-ver` are the borrower's Sequentia payout
program -- where the principal is paid and where a repayment refunds to -- and
`--lender-prog` and `--lender-ver` the lender's; both are 20 bytes at witness
version 0 and 32 at version 1, and both are baked into addresses, so neither can
be changed afterwards. `--reclaim-address` is the Bitcoin address the collateral
comes back to, a fresh one from the wallet by default. `--feerate` prices the
Bitcoin funding in sat/vB. `--btc-fee` is the flat satoshi fee the transactions
spending the vault carry, and `--upgrade-fee` is what the pre-vault holds on top
of the collateral so the move into the vault can pay for itself even if the
borrower has gone by then.

Several of these commands refuse before they act -- terms whose deadlines leave
no margin, a claim too shallow to spend against, a timelock that has not opened
-- and take `--force` to proceed anyway. Read what the refusal said before using
it: those checks are most of what stands between an over-collateralised loan and
a stall that pays the other side.

The other endings: `btc-seize-sighash` / `btc-seize` (lender + oracle),
`btc-timeout` (lender, after the term), `btc-refund` (borrower, if the lender
never claims the repayment), `btc-abort` (borrower, if the principal never
came), `btc-refund-principal` (lender, if the borrower never claimed it). The
trust model, the exposure at each step and why liquidation needs the oracle to
co-sign on the Bitcoin side are in the design doc, section 7.

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

| flag | what it does |
|---|---|
| `--config` | the JSON holding the key, both nodes' credentials and the state file |
| `--watch` / `--interval` | keep running, and how many seconds between passes (default 5) |
| `--disburse-conf` | Bitcoin confirmations required on the collateral before a principal is paid (default 1) |
| `--claim-depth` | confirmations required on a borrower's claim before their collateral is moved into the vault (default 6) -- the claim is a Sequentia transaction, and Sequentia reorgs when Bitcoin does |
| `--scan-interval` | seconds between chain scans for a repayment whose borrower never said where it landed (default 300) |
| `--fee-asset` | what the Sequentia legs pay their fee in (default: the debt asset) |
| `--state` | where it records what it has already done (default: beside the key) |

Every offer is signed by the key it names as the lender, and the relay verifies
that before storing it -- otherwise anyone could publish in a lender's name and
have that lender's own responder pay it out. The same goes the other way: every
report a responder makes about a take is signed, and a borrower's page checks it.

A borrower takes an offer from the page's BTC tab, or from the command line:

```
pignus-cli btc-offer-take --offer <id> --borrower-key borrower.key \
    --borrower-prog <hex> --out loan.json --btc-rpc ... --rpc ...
```

That is the whole borrower's half of the handshake in one command: it checks
the offer's own signature, refuses an offer whose oracle is the lender's key,
funds the pre-vault without broadcasting it, waits for the lender's hash, signs
the move into the vault that hash implies, waits for the release, verifies it
locally, and only then broadcasts. Every refusal along the way leaves the
Bitcoin untouched. `--wait` bounds how long it will sit waiting for a lender.
It writes the same ticket file the two-party commands use, so `btc-check` and
the rest work on the loan afterwards.

The relay itself is never trusted: it holds no key, moves nothing, and rebuilds
every address, outpoint and sighash from the offer's own terms rather than
believing what it is told. [`docs/api.md`](docs/api.md) documents each endpoint.

### Restricted assets (Tiers C and D)

Two asset models on Sequentia cannot use the covenant vault, and each gets a
different answer rather than a pretence. The loan book carries neither. Tier C
is command-line only; Tier D has a *Check a repurchase* tab on the page that
reads a terms document and its bond back, while everything that moves money is
a command.

**OpenAMP (Tier C)** collateral never moves. The issuer's policy server records
a **pledge** against part of the borrower's balance and refuses transfers that
would spend it, so `pledge-create`, `pledge-list`, `pledge-release` and
`pledge-seize` speak to that server rather than to a node. `pledge-sign` is what
lets a party authorise a release or a seizure on their own machine, so a
signature travels instead of a key: pass the result as `--lender-sig` or, for
the borrower's countersignature on an early seizure, `--holder-sig`. Every one
of these prints the sentence that says the collateral is issuer-permissioned,
because presenting it quietly beside a Tier A loan would be a lie. `--issuer`
names the `openampd` (default `http://127.0.0.1:8722`, or `PIGNUS_ISSUER`) and
`--token` authenticates the caller to it and nothing more (`PIGNUS_ISSUER_TOKEN`
is where it belongs).

**OpenDAMP (Tier D)** assets cannot be collateral at all, so what Pignus offers
is a **repurchase**: the borrower sells the asset and holds a claim, secured by
a bond in a two-leaf covenant vault. `repo-propose` writes the terms,
`repo-fund` pays the bond in, and `repo-forfeit` sweeps it back to the borrower
once the deadline passes.

```
pignus-cli repo-verify terms.json --txid <bond funding> \
    --leg-txid <the transfer to the lender> --lender-cu <hex>
```

`repo-verify` is the check that matters, and it reports a **state** rather than
an "ok": `not-funded`, `bond-only` (the bond is there and nobody has looked at
the half it secures), `funded-unburied`, `live`, `forfeitable`, or `settled`. A
bond alone is worth nothing, which is why the leg-one arguments are what move it
past `bond-only` and why `--min-confirmations` decides when either half stops
being reorgable.

Settling is one atomic transaction of four inputs and at most six outputs --
exactly what OpenDAMP allows, with no spare slot in either direction -- so it is
composed in two steps and signed in between:

```
pignus-cli repo-settle terms.json --txid <bond> --verifier <txid:vout> \
    --verifier-spk <hex> --cu-lender <txid:vout> --debt-utxo <txid:vout> \
    --skeleton settle.json
pignus-cli repo-settle terms.json --txid <bond> --attach settle.json --broadcast
```

`--skeleton` writes the unsigned settlement for the other party to sign;
`--attach` puts the covenant's `RETURN` witness on last, which is the only order
that works. The borrower's debt-asset side must be a single coin and the fee
comes out of it, because there is no room for another input.

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

`pignusd --config <file> --once` refreshes once and prints the markets, the
stats and the health, without serving, which is the way to check a
configuration before it is a unit. It refuses to start at all if the covenant
builder cannot be loaded or has drifted from the golden vectors, because every
vault address it would show is derived from that builder.

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
| `source.prices` | with `static`, the fixed price per market |
| `source.url`, `.field` | where the prices are, and which field of each row holds one |
| `source.timeout`, `.max_age` | seconds to wait, and how long a fetched snapshot may be reused |
| `source.feed_max_age` | how old the feed's own `_meta.updated` may be before this oracle refuses to re-sign its numbers |

8730 is the built-in listen default; the testnet box runs the oracle on 8740
and `pignusd` on 8741, see `deploy/DEPLOY.md`.

`--once` signs one round, prints it and exits, without serving; `--print-pubkey`
prints the x-only key and exits, and is the one path that will not create a key
file, because asking what the key is must never answer with a new one.

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
a number somebody else computed. `--sighash`, `--market` and `--strike` are the
hand-fed alternative, and they are worse for exactly that reason. `--max-age`
(600 seconds by default) is how recent the justifying price must be, and
`--allow-stale` co-signs against an older one.

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
  are bound by one hash carried in both chains' scripts, so neither can happen
  without the other -- but a lender who simply declines to claim the repayment
  keeps the collateral, which on an over-collateralised loan is worth more.
  Liquidation needs the oracle to co-sign, because Bitcoin has no introspection.
  Section 7.2 explains why that is not a DLC, and 7.1's exposure table says
  exactly what each party can lose.
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

Offline, no node. This half also runs in CI on every push:

```
tests/cli_drill.sh                 every CLI command, refusals included
tests/service_drill.sh             the oracle and pignusd together
tests/test_units.py                the covenant vectors + an oracle round trip
tests/test_openamp.py              the Tier C pledge message, pinned
tests/test_watcher.py              reorgs, and reading an exit back
tests/test_oracle_service.py       what the oracle will not sign
tests/test_liquidator.py           what the liquidation bot refuses
tests/test_btc_relay.py            the relay and the lender's responder
tests/test_btc_relay_auth.py       what the relay may be believed about
tests/test_web.mjs                 web/pignus.js against the golden vectors
tests/test_offer_web.mjs           web/offer.js against them
tests/test_repurchase_web.mjs      web/repurchase.js against them
tests/test_btc_web.mjs             web/btc.js against web/btc_vectors.json
tests/test_adaptor_web.mjs         web/adaptor.js against adaptor_vectors.json
tests/test_btcborrow_web.mjs       what the browser's BTC borrow flow refuses
tests/page_check.sh                the page, in a real browser; it skips
                                   itself where there is no headless Chromium
```

Against a running chain (a `sequentiad`, and for the BTC ones a `bitcoind` too):

```
tests/test_btc_disburse.py         paying the principal
tests/test_platform.py             this library, end to end
tests/test_pset.py                 the browser's PSETs, accepted by a node
tests/test_flows.py                the browser's flows through a whole loan
tests/test_book.py                 the book and the watcher against a chain
tests/test_watcher_reorg.py        the watcher against a real reorg
tests/test_tiers.py                Tiers C and D
tests/test_lifecycle.py            the CLI through fund, take, repay,
                                   liquidate, withdraw, default, with the
                                   daemon discovering every step
tests/test_threshold.py            a 2-of-3 oracle loan, end to end
tests/test_btc_collateral.py       Tier B's covenant and crypto
tests/test_btc_cli.py              the BTC-collateral library legs
tests/test_btc_cli_flow.py         the BTC-collateral CLI handshake
tests/test_prevault.py             origination on the Bitcoin side: the
                                   pre-vault, the upgrade, the abort
tests/test_btc_origination.py      a whole cross-chain loan, both chains,
                                   nobody trusting anybody
```

`tests/test_pset.mjs` and `tests/test_flows.mjs` are the browser halves of
`test_pset.py` and `test_flows.py` and are driven by them, because both need a
node behind them. `run-tests.sh` fails if a file in `tests/` is run by nothing
at all: a test nobody runs is a test nobody notices going red.

In the node repository, and in its `test_runner.py`:

```
test/functional/feature_pignus_vault.py      the covenant's exits and refusals
test/functional/feature_pignus_oracle_set.py the on-chain oracle set
test/functional/feature_pignus_offer.py      funded offers
test/functional/feature_pignus_hashlock.py   the signature-free hashlock both
                                             cross-chain legs are paid through
test/functional/feature_pignus_attack.py     the attack suite
```

`tests/test_platform.py` needs the `test/config.ini` a built checkout's
`configure` writes, and is skipped without one; the rest of the chain tests
start their nodes through `tests/rig.py` and need only the binaries.

`tests/gen_web_vectors.py` regenerates the golden vectors the browser's Bitcoin
and adaptor code pins itself to. Run it only when the Python it mirrors changes,
in the same commit, and re-run `test_btc_web.mjs` and `test_adaptor_web.mjs`.
