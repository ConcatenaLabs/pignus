# Pignus

Non-custodial collateralised lending on Sequentia. Borrow USDX against GOLD,
SILVR, OILX, tSEQ or any other unrestricted issued asset; the loan's terms are
compiled into a covenant and enforced by the script interpreter, not by an
operator.

Design and security analysis: `doc/sequentia/pignus-design.md` in the [node repository](https://github.com/GracedEternalKingCabbageMan/Sequentia).

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
pignus/compat.py    imports the PROVEN covenant and refuses a drifted one
pignus/terms.py     LoanTerms: the agreement, the address, and verify_funding()
pignus/oracle.py    attestation format, signing, verification, price quoting
pignus/vault.py     the origination and all four exit transactions
pignus/watcher.py   reconcile loans to the chain; name each exit; catch ghosts
pignus/node.py      a thin JSON-RPC client
bin/pignus-oracle   sign prices on a timer and publish them
bin/pignus-cli      propose, show, address, verify, status, check-attestation
bin/pignus-liquidator  one liquidator among however many people run one
```

There is **one** implementation of the covenant, in
`test/functional/pignus_covenant.py`, proven against a node by
`feature_pignus_vault.py`. This package imports it rather than porting it: a
port that differs by a single byte derives a different address, and the failure
mode of a wrong vault address is collateral nobody can ever spend.
`pignus/vectors.json` exists for implementations that genuinely cannot import
Python -- a browser wallet, a Go daemon -- and `compat.verify_builder()` uses it
here as a tripwire, refusing to derive addresses from a builder that has changed.

## Running it

The package needs a Sequentia **source** checkout, because that is where the
proven covenant lives. It finds it automatically when run from inside one;
otherwise set `SEQUENTIA_SRC`.

```
tests/cli_drill.sh        # offline, no node, ~2 seconds
pignus-cli selftest                      # vectors + an oracle round trip
```

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

prints `300000000`, which reads as 300 USDX atoms per GOLD atom, i.e. 3,000 USDX
per GOLD, along with the strike each loan-to-value ratio implies.

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
test/functional/feature_pignus_vault.py      the covenant: 4 exits, 11 refusals
tests/test_platform.py                       this library, end to end
tests/cli_drill.sh            the commands, offline
```

`feature_pignus_vault.py` is in the node repo's `test_runner.py`.
