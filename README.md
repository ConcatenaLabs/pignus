# Pignus

Non-custodial collateralised lending on Sequentia. Borrow USDX against GOLD,
SILVR, OILX, tSEQ or any other unrestricted issued asset; the loan's terms are
compiled into a covenant and enforced by the script interpreter, not by an
operator.

Design and security analysis: [`docs/pignus-design.md`](docs/pignus-design.md).

## What it actually guarantees

A borrower locks collateral in one taproot UTXO with a NUMS internal key -- so
there is no key path -- and four leaves that are the only ways out:

| leaf | who | needs | does |
|---|---|---|---|
| `REPAY` | anyone | nothing at all | pay the lender the debt, return the whole collateral to the borrower |
| `LIQUIDATE` | anyone | an oracle attestation under the strike | pay the lender, keep the bonus, return the surplus |
| `DEFAULT` | anyone | an attestation, after maturity | the same seizure, at any price |
| `RECOVER` | the lender | a long timeout after maturity | sweep the vault: the oracle-liveness backstop |

Every term -- both asset ids, the total repayment, both payout scriptPubKeys,
the oracle key, the price feed, the strike, the maturity, the bonus -- is a
constant inside those leaves, and the leaves are committed inside the taproot
output key. So the terms and the address are the same fact stated twice, and
that is what makes the one check below sufficient.

`REPAY` needs no signature, no oracle and no witness data. A solvent borrower
can always leave, whatever anyone else does.

## The one check

```
pignus-cli verify --terms loan.json --txid <funding> --vout 0
```

It rebuilds the vault address from the terms you agreed and compares it to the
output actually being funded, and it asserts the internal key is NUMS. A loan
whose debt is one atom different, or whose lender, oracle, market or strike has
been swapped, compiles to a different address and is refused.

Run this before signing an origination. Everything Pignus claims reduces to it;
a wallet or a book that skips it has quietly reintroduced a trusted party.

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
pignus/btc_collateral.py native BTC collateral (Tier B): the Bitcoin-side leaves
pignus/adaptor.py        Schnorr adaptor signatures, the cross-chain link
pignus/dlc.py            the DLC that settles BTC collateral at maturity
pignus/btcscript.py      the Bitcoin script and taproot primitives Tier B needs
pignus/openamp.py        Tier C pledges at an OpenAMP policy server
pignus/repurchase.py     Tier D: the OpenDAMP repurchase, labelled as one, never a loan
pignus/node.py           a thin JSON-RPC client
bin/pignus-oracle        sign prices on a timer and publish them
bin/pignusd              the loan book, the watcher, and the page at /lending/
bin/pignus-cli           selftest, quote, propose, show, address, verify, status,
                         check-attestation; with a node wallet: offer-fund,
                         offer-take, offer-withdraw, repay, liquidate, default,
                         recover; pledge-* (Tier C); repo-* (repurchase)
bin/pignus-liquidator    one liquidator among however many people run one
web/                     the browser client pignusd serves: pignus.js, offer.js,
                         repurchase.js, pset.js, flows.js, wallet.js, app.js
deploy/                  the two systemd units, example configs, DEPLOY.md
docs/pignus-design.md    the design and security analysis
```

There is one **proven** implementation of the covenant, in the node
repository's `test/functional/pignus_covenant.py`, proven against a node by
`feature_pignus_vault.py`. This package imports it rather than porting it: a
port that differs by a single byte derives a different address, and the failure
mode of a wrong vault address is collateral nobody can ever spend.
`pignus/vectors.json` exists for implementations that genuinely cannot import
Python: `web/pignus.js` and `web/offer.js` are that second implementation, for
the browser, pinned byte for byte to the same vectors, and the page refuses to
run if the pinning fails. `compat.verify_builder()` uses the vectors here as a
tripwire, refusing to derive addresses from a builder that has changed.

The book follows the chain on its own. An offer's coin is watched; when it is
taken, the borrower's payout program is read out of the take witness, the new
vault is registered as a loan, and the offer moves to its remainder. A loan
taken by any wallet, through the page or not, turns up on the page.

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
`http://127.0.0.1:8741`); `--rpc*` the node wallet that signs.

### Threshold oracles

An m-of-n loan bakes in several independent oracles and needs `threshold` of
them to agree before it can be liquidated:

```
pignus-cli offer-fund --market GOLD/USDX --principal 100 \
    --oracles book --oracle-threshold 2 --rpc-wallet me
```

`--oracles book` uses every oracle the book quotes against; the CLI, the
liquidator and the browser all assemble the threshold witness.

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
pignus-cli btc-propose --lender-key lender.key --borrower-x <x> --oracle-x <x> \
    --btc-amount 100000 --debt-asset <id> --debt 5000000000 \
    --recover-after <btc-height> --repay-deadline <seq-height> --out loan.json
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

A lender who wants their offers taken while they sleep runs the responder, which
signs releases, pays principals, starts loans as borrowers claim them and takes
back what nobody claimed:

```
pignus-cli btc-respond --config responder.json --watch
```

The configuration file carries the node credentials and the path to the lender's
key, so nothing secret is on the command line, where every user on the machine
can read it. `deploy/responder.example.json` is the starting point and
`deploy/pignus-btc-responder.service` runs it.

## Running it

The package needs a Sequentia **source** checkout, because that is where the
proven covenant lives. It finds it automatically when run from inside one;
otherwise set `SEQUENTIA_SRC`.

```
tests/cli_drill.sh        # offline, no node, ~2 seconds
pignus-cli selftest                      # vectors + an oracle round trip
```

`tests/test_btc_collateral.py` also needs a Bitcoin Core `bitcoind`
(`PIGNUS_BITCOIND`, default `~/bitcoin-28.0/bin/bitcoind`), and the
`tests/*.mjs` browser checks need Node.

### The book and the page

`pignusd` serves the loan book, the chain watcher and the browser client, and
on the testnet it is what `/lending/` is. `deploy/DEPLOY.md` covers running it
and the oracle as systemd units behind Caddy, with `deploy/pignusd.example.json`
as the starting configuration.

### The oracle

```
pignus-oracle --config oracle.json
```

```json
{
  "keyfile":     "/var/lib/pignus/oracle.key",
  "logfile":     "/var/lib/pignus/attestations.log",
  "listen":      "127.0.0.1:8730",
  "interval":    60,
  "price_scale": 100000,
  "markets":     ["GOLD/USDX", "SILVR/USDX", "OILX/USDX"],
  "source":      {"type": "http", "url": "http://127.0.0.1:8088/price"}
}
```

8730 is the built-in listen default; the testnet box runs the oracle on 8740
and `pignusd` on 8741, see `deploy/DEPLOY.md`.

The key is created 0600 on first run and its mode is re-checked on every start.
It is never logged and never served. Prices come from the price feed that
already drives the any-asset fee market (`contrib/price-server`) -- deliberately
not a second price pipeline.

Endpoints: `/v1/pubkey`, `/v1/markets`, `/v1/attestation/{market}` (use `_` for
the slash), `/v1/log`, `/v1/digest`, `/healthz`.

### Prices

A price is **debt-asset atoms per collateral-asset atom, scaled by
`price_scale`** (default `1e5`). Quoting per atom is what keeps the covenant
ignorant of either asset's decimals.

```
pignus-cli quote --market GOLD/USDX --collateral-ref 3000 --debt-ref 1
```

prints `300000000`: 3,000 USDX atoms per GOLD atom once the 1e5 scale is
divided out, i.e. 3,000 USDX per GOLD. It also prints the strike each
loan-to-value ratio implies.

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

Two collateral types are weaker on purpose and are labelled as such:

- **Native BTC** (design doc section 7) is cross-chain. Repayment is trustless
  via an adaptor signature, but liquidation needs the oracle to co-sign, because
  Bitcoin has no introspection. Section 7.2 explains why that is not a DLC.
- **OpenAMP restricted assets** can only live in shapes their issuer permits, so
  every exit needs the policy server to co-sign. That is *not* non-custodial in
  the sense above, it is inherent to a transfer-restricted asset, and any UI
  showing such a loan must say so.

## Tests

```
test/functional/feature_pignus_vault.py      the covenant: 4 exits, 12 refusals
test/functional/feature_pignus_oracle_set.py the on-chain oracle set
test/functional/feature_pignus_offer.py      funded offers
test/functional/feature_pignus_attack.py     the attack suite
tests/test_platform.py                       this library, end to end
tests/test_btc_collateral.py                 Tier B, on a bitcoind + sequentiad rig
tests/test_lifecycle.py                      the CLI through fund, take, repay,
                                             liquidate, withdraw, default, with
                                             the daemon discovering every step
tests/test_threshold.py                      a 2-of-3 oracle loan, end to end
tests/test_openamp.py                        the Tier C pledge message, pinned
tests/test_btc_cli.py                        the BTC-collateral library legs
tests/test_btc_cli_flow.py                   the BTC-collateral CLI handshake
tests/run-tests.sh                           everything here, fastest first
tests/cli_drill.sh            the commands, offline
```

The four `feature_pignus_*.py` tests live in the node repository and are in its
`test_runner.py`.
