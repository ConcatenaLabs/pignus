# Pignus: non-custodial collateralised lending on Sequentia

Status (2026-08-22): the loan-vault covenant (section 2) is implemented in the
node repository's
[`test/functional/pignus_covenant.py`](https://github.com/GracedEternalKingCabbageMan/Sequentia/blob/master/test/functional/pignus_covenant.py)
and proven against the node by `feature_pignus_vault.py`, which is in the test
runner, with the oracle set (6.1), funded offers (3) and the attack suite proven
by `feature_pignus_oracle_set.py`, `feature_pignus_offer.py` and
`feature_pignus_attack.py` beside it. The oracle, the loan book, the watcher,
the browser client, the Bitcoin-collateral construction (section 7) and the
OpenDAMP repurchase (8.1) are implemented in this repository. The oracle, the
loan book and the browser client run on the testnet at
[sequentiatestnet.com/lending/](https://sequentiatestnet.com/lending/), and
`pignus-cli` is on the download page.

*Pignus* is the Roman-law term for property pledged as security for a debt: the
creditor holds the pledge, the debt is owed separately, and redeeming the debt
redeems the pledge. That is exactly the shape of the thing, and it is a working
name -- renaming costs one identifier.

Companion documents, all in other repositories:
[`openamp-design.md`](https://github.com/GracedEternalKingCabbageMan/Sequentia/blob/master/doc/sequentia/openamp-design.md) and
[`opendamp-design.md`](https://github.com/GracedEternalKingCabbageMan/Sequentia/blob/master/doc/sequentia/opendamp-design.md) in the node
repository (the two restricted-asset models this coexists with, section 8),
[`simplicity-dex-covenant-offers-design.md`](https://github.com/GracedEternalKingCabbageMan/seqdex/blob/main/docs/simplicity-dex-covenant-offers-design.md)
in the `seqdex` repository (the covenant-offer design this borrows its
output-map and self-replication techniques from), and
[`03-bitcoin-anchoring.md`](https://github.com/GracedEternalKingCabbageMan/Sequentia/blob/master/doc/sequentia/03-bitcoin-anchoring.md) in
the node repository (why section 6.4 exists at all).

## 1. What is being claimed

A borrower locks collateral in a single taproot UTXO and receives principal in
USDX. The vault's spending rules are the loan agreement, compiled. Precisely:

- **No custodian.** The collateral is never held by a platform, a lender, a
  multisig federation or an issuer. It sits in a UTXO with a NUMS internal key,
  so there is no key path, and the only ways out are the four leaves.
- **No term can be restated.** Both asset ids, the total repayment, both payout
  scriptPubKeys, the oracle key, the price feed, the strike, the maturity and
  the liquidation bonus are compile-time constants inside the leaves, and the
  leaves are committed inside the taproot output key. Changing any of them
  changes the address, so the borrower verifies terms by reconstructing the
  address before funding it.
- **No permission to exit.** REPAY is permissionless, needs no signature, no
  oracle and no witness data whatsoever. A solvent borrower can always leave.
  In fact NO exit needs a signature: every leaf reads what it enforces out of
  the transaction and pays a pinned destination, so there is no key anywhere in
  a loan whose loss costs anybody anything.
- **No discretionary seizure.** A liquidator cannot choose how much to take.
  The seizure is computed on chain from the attested price and the surplus is
  forced back to the borrower by the same script that lets the seizure happen.

What is *not* claimed: the price is an external fact and something has to
assert it. Section 6.1 states exactly how much power that gives the oracle, and
it is much less than "can take the money".

## 2. The vault covenant

A taproot output, internal key = the BIP341 NUMS point, tapleaf version 0xc4
(Elements tapscript), four leaves. Reference implementation:
[`test/functional/pignus_covenant.py`](https://github.com/GracedEternalKingCabbageMan/Sequentia/blob/master/test/functional/pignus_covenant.py)
in the node repository.

Notation: `C` is the collateral asset, `D` the debt asset, `L` the collateral
amount in the vault, `debt` the total repayment (principal plus the whole
term's interest, fixed at origination -- these are term loans, so no interest
accrues on chain).

### 2.1 The output map

The covenant input at consensus index `k` credits the lender at output `2k` and
returns collateral to the borrower at output `2k+1`, with the index recomputed
per input from `OP_PUSHCURRENTINPUTINDEX`. This is the anti-aliasing device from
the SeqOB FILL leaf: because each input derives its own output pair, two vault
inputs can never both point at one shared credit output, so a single payment
cannot settle two loans. The functional test spends two vaults in one
transaction against one credit to prove it.

Every introspected asset and value prefix must be `0x01`. A blinded output is
rejected rather than guessed at -- a covenant cannot police a value it cannot
read. On transparent-by-default Sequentia this is the ordinary case, not a
privacy regression; a borrower who wants a confidential loan needs an
interactive tier instead, which is out of scope here.

### 2.2 REPAY -- permissionless, oracle-free, witness-free

Spendable iff output `2k` pays the lender at least `debt` of explicit asset `D`
at the pinned scriptPubKey, and output `2k+1` returns the whole of `L` in asset
`C` to the borrower's pinned scriptPubKey.

No signature and no witness data: the leaf reads everything it needs from the
transaction, so the witness is just `[leaf, control_block]`. Anyone may repay --
the borrower, a friend, a refinancing bot competing to buy the position. Because
both destinations are pinned, a third-party repayer can only make the borrower
better off, which is why letting anyone do it is safe.

This leaf is the reason an oracle outage is survivable. Every other exit needs
either an attestation or a long timeout; this one needs nothing.

### 2.3 LIQUIDATE -- permissionless, oracle-attested

The witness supplies `[sig, price, ts]`. The leaf reassembles the 48-byte
message `feed_id || ts || price` with `OP_CAT` and checks it against the pinned
oracle key with `OP_CHECKSIGFROMSTACKVERIFY`, then requires `ts >= not_before`
and `price < strike`.

It then computes, on chain,

    gross = ceil(debt * bonus_num / bonus_den)      -- folded at build time
    seize = ceil(gross * price_scale / price)       -- OP_ADD64/OP_SUB64/OP_DIV64

and requires output `2k` to pay the lender `debt` and output `2k+1` to return
`L - seize` to the borrower. The liquidator keeps `seize`, whose value at the
attested price is `gross` -- the debt plus the bonus that pays for the work --
overshooting by less than the value of one collateral atom because the ceiling
rounds the last atom the liquidator's way rather than letting a rounding loss
strand the position.

The signature is checked over exactly the numbers the script then computes with.
There is no second, unauthenticated copy of the price anywhere in the spend.

For an underwater vault `L - seize` is negative, the comparison against a zero
return passes, and the liquidator takes everything -- correct, since the
collateral no longer covers the debt.

### 2.4 DEFAULT -- permissionless, oracle-attested, after maturity

`<maturity> CLTV DROP` followed by the same attestation check and the same
seizure tail, minus the strike test. Once the term is up the debt is due at any
price, so anyone may call the loan; the covenant still forces the surplus home,
which is what makes it safe to let anyone do it. The functional test calls a
loan at a price *above* the strike, so the spend provably could not have been a
liquidation.

### 2.5 RECOVER -- the oracle-liveness backstop

`<recover_after> CLTV DROP` and then the same pinned-payout check the other
leaves use: after the backstop height ANYONE may sweep the vault, but only to
the lender's payout program.

This is the one blunt leaf and it is deliberately last. It exists because a dead
oracle must not freeze collateral for ever, and it is acceptable because the
borrower has the entire term to take the oracle-free REPAY exit and only reaches
this leaf by ignoring it long after maturity. `recover_after` should sit far
enough past `maturity` that a transient oracle outage cannot reach it;
`maturity + 30 days` is the suggested default, and the borrower must check the
gap before funding, because it is the borrower who pays for a short one.

It used to require the lender's signature, and that was wrong. The browser
wallet extension signs its own transaction inputs but cannot sign a covenant
leaf, and exposes no x-only key to bake into one -- so a lender using a browser
had a backstop nobody could execute, which is not a backstop but a trap holding
their collateral. Pinning the destination fixes that and is better on its own
terms: the lender needs no key beyond the address they are paid at, so there is
nothing to lose, and they need not be online, for the same reason REPAY is
permissionless. Letting anyone trigger it is safe because it can only ever pay
the lender.

**No exit in the system needs a signature.** Every leaf reads what it enforces
out of the transaction and pays a pinned destination. That is what makes the
whole thing drivable from a browser wallet, and it means there is no key
anywhere in a loan whose loss costs anybody anything.

### 2.6 Where the payouts go

A payout is a witness PROGRAM and a witness VERSION, both baked in, not an
address and not necessarily taproot. The version matters more than it sounds:
the browser wallet extension is a `wpkhSlip77` wallet and every address it can
receive at is segwit v0, so a covenant that could only pay a v1 taproot program
could never settle a loan originated from a browser -- the lender could not be
repaid and the borrower could not get their collateral back.

So `lender_ver` and `borrower_ver` default to 1 and may be 0, and the builders
refuse a program whose length does not match its version (20 bytes at v0, 32 at
v1). That refusal is not fussiness: a mismatched program compiles silently into
an address nobody can ever be paid at, and the loan looks perfectly healthy
until somebody tries to leave it.

### 2.7 Sizes and limits

Measured leaf sizes: REPAY 192 bytes, LIQUIDATE 352, DEFAULT 345, RECOVER 39.
A REPAY spend is in the same class as a covenant CLOB fill (a few hundred
vbytes; the measured user capacity of a block is about 89,999 vB), which
matters: doing
this in Simplicity instead would cost 7,459 vB and cap the platform at 12
operations per block. Tapscript introspection is the right tool and needs no
consensus change at all -- 0xc4 is gated only by always-active
`SCRIPT_VERIFY_TAPROOT`.

**64-bit bound.** `OP_ADD64` aborts on signed-64-bit overflow. The only large
value formed on chain is `gross * price_scale + price - 1`, so the builders
assert `gross * price_scale + bound < 2^63`, where `bound` is the strike for
LIQUIDATE and a caller-declared `max_price` for DEFAULT. A vault that could
abort mid-spend cannot be constructed -- the builder refuses at origination
rather than leaving a loan that cannot be liquidated.

The ceiling this puts on a single loan, at 8 decimal places and the default 5%
bonus:

| `price_scale` | max debt (units) | price precision |
|---|---|---|
| `1e5` (default) | ~878,000 | 1e-5 relative |
| `1e4` | ~8,784,000 | 1e-4 relative |
| `1e3` | ~87,841,000 | 1e-3 relative |

So the default caps one loan at roughly 878,000 USDX, and a larger loan trades
price precision for size by lowering `price_scale`. Neither knob is a protocol
limit: two loans are two vaults, and nothing stops a borrower opening several.
The numbers are worth stating because the first instinct -- "64 bits is plenty"
-- is wrong here: the seizure forms `gross * price_scale`, and at 8 decimals
that product eats 60 of the 63 available bits by itself.

## 3. Origination

One atomic transaction, signed by both parties, no escrow:

    inputs   borrower's collateral utxo(s), lender's principal utxo(s)
    outputs  0: the vault (L of C at the covenant address)
             1: principal (the loan amount of D) to the borrower
             +  changes, and the network fee output

Either party can walk away before signing and nobody is ever exposed to the
other. This is the same PSET co-signing flow SeqDEX already uses for same-chain
atomic swaps, so it is proven machinery rather than new machinery.

The borrower MUST, before signing, reconstruct the vault address from the terms
and check it equals output 0's scriptPubKey, and check the internal key is NUMS.
That single check is what makes everything in section 1 true; a wallet that
skips it has silently reintroduced a trusted party. `pignus-cli verify` and the
wallet integration both do it, and the daemon never asks a user to sign a vault
it did not reconstruct locally.

**Offers.** Lenders publish signed offers (asset pair, size, rate, term, strike,
oracle) to the book; borrowers take one. The book is pure discovery -- it holds
no funds and cannot alter terms, because the terms are inside the address the
borrower reconstructs. A *funded* resting offer, where the lender's principal
sits in its own covenant that anyone may take by locking a correctly-shaped
vault in the same transaction, is implemented in the node repository's
[`test/functional/pignus_offer.py`](https://github.com/GracedEternalKingCabbageMan/Sequentia/blob/master/test/functional/pignus_offer.py)
and proven by `feature_pignus_offer.py`; `pignus/offers.py` and `web/offer.js`
drive it. The offer covenant recomputes the vault's taproot address from a
witness-supplied borrower key with `OP_TWEAKVERIFY` plus the tagged hashes, the
technique OpenAMP's containment covenant proved.

## 4. The oracle

The oracle signs

    msg = feed_id (32) || timestamp (8, LE) || price (8, LE)

with BIP340, and that is the entire protocol. `feed_id` is the hash of the
canonical market name, so an attestation for one market cannot be replayed
against another -- the functional test proves a genuine attestation for a
different feed is refused as an invalid signature, because the feed is inside
the signed message.

`price` is quoted as debt-asset atoms per collateral-asset atom, scaled by
`price_scale` (default `1e5`). Quoting per *atom* rather than per unit means the
covenant never has to know either asset's decimal precision.

Prices come from the existing price infrastructure (`contrib/price-server`,
which already feeds the any-asset fee market from a real quote source). Pignus
adds only a signer and a publication endpoint; it deliberately does not add a
second price pipeline.

The oracle is a public, replayable log: attestations are published for everyone,
not handed to a liquidator, so any watcher can verify a liquidation was
justified after the fact.

## 5. Freshness, and the one honest gap

The covenant can test that an attestation is NEWER than `not_before`. Nothing in
tapscript can test that it is RECENT: there is no way to read the current time
inside a script and compare it to a witness-supplied value. `OP_CHECKLOCKTIMEVERIFY`
sets a *lower* bound on the transaction's locktime, which is the wrong
direction.

So a liquidator who saved a signed attestation from a genuine dip may present it
later, after the price has recovered. What this is and is not:

- It is **not theft.** The position genuinely was liquidatable at that moment.
  The liquidator pays the full debt and the surplus is still forced back to the
  borrower at the *attested* price -- and since that price was lower than the
  current one, the borrower's surplus is computed less favourably than it would
  be today. The loss to the borrower is bounded by the difference between the
  dip price and the current price, on the seized portion only.
- It is a **timing advantage**, and the borrower's cure is the same either way:
  repay, or top up before the dip.

Two mitigations, in increasing cost:

1. **Epoch commitment (recommended, oracle-side).** The oracle signs
   `feed_id || epoch || price` where `epoch` advances every N minutes, and the
   vault bakes a `min_epoch`. This does not remove the window, it bounds how far
   back a saved attestation can reach only if `min_epoch` advances -- which
   requires re-covenanting. Useful mainly for short-term loans, where
   `not_before` can be set close to origination.
2. **Re-covenanting on top-up.** A borrower adding collateral moves to a fresh
   vault with a later `not_before`, which retires every attestation older than
   the top-up. This is the practical answer and it falls out of the design for
   free: a top-up is a REPAY-and-reopen, or an explicit new origination.

This gap is inherent to putting an external fact into a script, and every
oracle-driven on-chain lending design has some version of it. It is written down
here rather than left for someone to find.

## 6. Trust surface

### 6.1 What the oracle can and cannot do

Can: assert a price low enough to open LIQUIDATE. That is the whole of its
power.

Cannot: move funds; choose who receives anything (both payouts are pinned);
change how much is seized (it follows from the price it attested, and attesting
a lower price *shrinks* the liquidator's seizure per atom while enlarging it in
count -- the value seized is always `gross`); trigger a default before maturity
(CLTV); stop or delay a repayment (REPAY does not consult it); or keep a
borrower's surplus (the covenant forces it home).

The worst a fully malicious oracle achieves is liquidating solvent positions at
a fabricated low price, which costs the borrower the liquidation bonus and the
difference between the fabricated and the true price on the seized portion --
bad, publicly evident from the signed log, and bounded. It cannot steal the
collateral.

Hardening comes in two shapes, and both are implemented.

**A threshold group key** (FROST, the `PolicySigner` seam OpenAMP already
built): the vault names ONE key, the m-of-n lives in the signing protocol, and
nothing on chain changes. Cheapest, and the group key's invariance under
resharing means the signer set can rotate without moving a single vault address.
Its cost is that the signers must run a joint protocol -- there is a coordinator,
and the signers' liveness is coupled.

**An on-chain oracle set** (`oracles=[...]`, `oracle_threshold=m`): the vault
names n keys and the covenant counts valid signatures itself. The oracles never
talk to each other, never learn that the others exist, and no coordinator can be
attacked. Its cost is script size -- the LIQUIDATE leaf grows from 352 to 633
bytes for 2-of-3.

The on-chain set does NOT require the oracles to agree on a byte. Each signs its
own `(timestamp, price)`, each accepted price must independently clear the
strike, and the price carried into the seizure is the **maximum** of the
accepted ones. That choice does two things: it is borrower-favourable (a higher
price seizes less collateral), and it makes shopping pointless, because
presenting an extra low attestation cannot drag the price down when the largest
of whatever is shown is what counts. A liquidator's best play is to present
exactly the `m` lowest attestations they hold, which makes the effective price
the m-th lowest of the set -- a robust quantile rather than any one oracle's
number.

Two properties fall out and are worth stating. A single compromised oracle can
no longer trigger a liquidation, because it cannot reach the threshold alone --
and a signature from one key replayed into another key's slot fails, since every
slot pins its own key. A single dead oracle can no longer block one either, so
the set improves both halves of the trust problem rather than trading one for
the other. An abstaining slot carries an EMPTY signature, which
`OP_CHECKSIGFROMSTACK` treats as false; a non-empty invalid signature aborts the
script instead, so a slot cannot be stuffed with rubbish to fake an abstention.

One trap the builder refuses outright: a 1-of-n set is weaker than a single
oracle, not stronger, because ANY of the n can act alone. `sanity_check()` says
so, and duplicate keys are rejected because one signer filling two slots makes
the threshold mean less than it says.

### 6.2 What the platform can do

Nothing. The book is discovery, the watcher is read-only, and the liquidator bot
is just the first participant to notice -- anyone can run one, and the covenant
does not care which one wins.

### 6.3 Liquidation races

Liquidation is a permissionless race, and the winner is whoever gets a valid
spend mined. This is an unpriced race, not a theft: every racer must pay the
lender in full and return the surplus, so the borrower and lender are
indifferent to who wins. The bonus is what prices the race.

### 6.4 Bitcoin anchoring

Sequentia reorgs when Bitcoin reorgs, in real time, and that outranks
everything. So a vault funding transaction can be undone by an anchor-driven
reorg, exactly as a covenant CLOB order can. The watcher therefore classifies a
vault whose funding has been reorged away as GHOST and drops it, the same way
`seqob-watcher` does, and a lender must not treat a loan as originated until its
funding is buried by the depth their risk appetite justifies. This is not a
Pignus-specific caveat; it is the chain's first principle, and any design that
pretended otherwise would be wrong.

## 7. Native Bitcoin as collateral

Sequentia uses **native** Bitcoin on the parent chain, not a pegged
representation, so BTC collateral means a real Bitcoin UTXO -- which has no
introspection, no `OP_CAT`, and no `OP_CHECKSIGFROMSTACK`. None of section 2
runs there. The construction is therefore cross-chain: collateral on Bitcoin,
debt on Sequentia, linked so that the two settle together.

### 7.1 The construction

Bitcoin side (`pignus/btc_collateral.py`): a P2TR funding output with the
NUMS internal key -- no key path, the same rule as the Sequentia vault -- and
three leaves:

- leaf RECLAIM: `<P_borrower> CHECKSIGVERIFY <P_lender> CHECKSIG` -- a 2-of-2,
  the repayment path;
- leaf SEIZE: `<P_lender> CHECKSIGVERIFY <P_oracle> CHECKSIG` -- lender and
  oracle jointly, the liquidation path;
- leaf TIMEOUT: `<recover_after> CLTV DROP <P_lender> CHECKSIG` -- the backstop.

Repayment is linked to the BTC release by an adaptor signature
(`pignus/adaptor.py`) on the lender's half of RECLAIM, which makes the solvent
path trustless:

1. The lender picks a secret `t` and publishes `T = t·G` and `h = SHA256(t)`.
2. At origination the lender hands the borrower their half of the RECLAIM
   signature on the transaction that returns the BTC to the borrower, as an
   **adaptor** signature under `T`. The borrower holds a release signature they
   cannot yet complete.
3. The borrower repays on Sequentia into a taproot output with two leaves:
   CLAIM, `OP_SHA256 <h> OP_EQUALVERIFY <P_lender> CHECKSIG`, and REFUND,
   `<repay_deadline> CLTV DROP <P_borrower> CHECKSIG` if the lender stalls.
4. The lender claims the repayment, which publishes `t` on the Sequentia chain.
5. The borrower reads `t`, completes the adaptor signature, adds their own half,
   and takes the BTC back through RECLAIM.

If the lender never claims, the borrower recovers the principal repayment after
the CLTV and the lender takes the BTC via TIMEOUT: the loan unwinds, nobody is
robbed, and the lender is strictly worse off for stalling, so they do not.

The claimant on the Bitcoin side must re-run the anchor-safety check on the
Sequentia reveal before acting on `t`, for the reason in section 6.4 -- a
covenant cannot introspect anchoring, so this stays a watcher discipline. This
is the same discipline the SeqDEX cross-chain leg already documents.

### 7.2 Why an oracle leaf and not a DLC

The obvious question, and the honest answer is that a DLC is the right tool for
a *settlement* and the wrong one for a *liquidation*.

A DLC pre-signs one contract execution transaction per discretised outcome, each
encrypted to an oracle attestation point, and settles at a **fixed maturity**.
That is a clean fit for "at date X, split the collateral according to the
price". A margin loan does not have that shape: it must be liquidatable the
moment the price crosses the strike, at any time during the term. Making a DLC
do that needs an oracle announcement per time step and a CET set per (time,
price) pair, which multiplies out instead of adding up -- and every one of those
CETs has to be pre-signed at origination, by both parties, before either knows
when the dip will come.

So Pignus uses the shape that fits each job:

- **Liquidation during the term** -- the 2-of-2 SEIZE leaf. The oracle must
  actively co-sign, which is a real and stated trust assumption, and the same
  one section 6.1 already bounds on the Sequentia side.
- **Settlement at maturity** -- a genuine DLC, built in `pignus/dlc.py` for the
  maturity path specifically, because there the outcome set is one-dimensional
  and the date is known. The oracle announces one public event, the BTC/USDX
  price at the maturity date, and attests it without knowing any loan exists;
  each side adaptor-signs one contract-execution transaction per price bucket,
  and the attestation is the decryption key for exactly one of them. That
  removes the oracle's per-loan involvement at maturity entirely.

Stating it plainly: BTC collateral is the one tier where the oracle is trusted
*interactively* rather than only for a number. That is the price of collateral
on a chain with no covenants, and it is why the unrestricted-asset tier is the
one to use where a choice exists.

## 8. Collateral tiers

| Tier | Assets | Enforcement | Trust |
|---|---|---|---|
| A | tSEQ, GOLD, SILVR, OILX, EURX, SBTC, and any unrestricted issued asset | The section-2 covenant | Oracle, for one number |
| B | Native BTC | Section 7, cross-chain | Oracle, interactively, for liquidation only |
| C | OpenAMP (`cosign`) assets | A pledge the policy server enforces | Oracle **and** the issuer |
| D | OpenDAMP (`damp`) assets | Not lending: a repurchase (8.1) | Oracle, and the lender for the bond |

**Tier A** is the design. USDX is the debt asset throughout; every unrestricted
asset can be collateral, and the fee for any of it is payable in any accepted
asset, because no asset is privileged here any more than anywhere else on
Sequentia.

**Tier C** deserves a blunt statement. A restricted asset can only live in the
shapes its issuer permits -- for OpenAMP, a 2-of-2 enclave output with the
policy server -- and a covenant vault is not one of them. Worse, if it were, the
covenant would hand the collateral to whoever satisfied the loan's terms, which
is exactly the transfer restriction the issuer exists to enforce. Self-enforcing
collateral and issuer-enforced transferability are in direct tension. You can
have one.

So the collateral does not move. What shipped in `openampd` is a **pledge**: a
record that part of a holder's balance stands behind a debt, and a transfer
check that refuses to let it leave. A holder may spend anything above their open
pledges and nothing below, and pledges accumulate, so a second lender is never
sold a claim on atoms a first lender already holds. The collateral stays in the
borrower's own enclave for the whole life of the loan.

The two operations that move value are deliberately not satisfied by the
issuer's own authority. A **release** needs the LENDER's signature, because the
lender is the only party who can say the debt was settled. A **seizure** needs
the lender's signature *and* either a matured loan or the BORROWER's
countersignature, so a lender cannot take collateral from a borrower still
inside their term. The issuer may force a release with a written reason -- a
lender who loses their key would otherwise lock the collateral forever, and a
forced release only ever returns it to its owner -- and there is deliberately no
forced seizure, because the direction that takes value away from its owner is
the direction that must not have a unilateral override. A seizure is an L_claw
spend with two asset outputs: the pledged atoms to the lender's enclave, the
remainder straight back to the borrower's, so settling one loan never disturbs
the borrower's free balance or another lender's collateral.

Pledging an asset issued *without* a clawback leaf is refused outright, because
the lock would hold but nobody could ever deliver on default: the collateral
would freeze permanently rather than secure anything.

What Tier C costs is not hidden anywhere: the lender's security is the issuer's
promise, not a script. A policy server that is dishonest or compromised can
release a pledge without repayment, and no amount of checking on the lender's
side would reveal it beforehand. The platform labels a Tier C loan
issuer-permissioned wherever a user can see it; presenting it quietly beside a
Tier A loan would be a lie.

**Tier D** is not open any more, and the answer is negative. A seizure-backed
loan against an OpenDAMP asset is impossible, for three independent structural
reasons, any one of which is sufficient:

1. **The collateral cannot enter a vault.** The verifier covenant requires that
   every output carrying the regulated asset pays `C_U(Y)` for a witness-supplied
   recipient key Y (opendamp-design.md 2.2, check 4). A Pignus vault script is
   not of that form and cannot be made to be. This is network-enforced, so it is
   not something an issuer could waive even if it wanted to.
2. **Exits cannot be pre-signed.** The DLC-style escape -- both parties sign the
   repay and the seize transactions at origination, and the lender broadcasts
   the seizure after maturity -- fails because every transfer spends the shared
   verifier output as input zero and returns it to the same address (2.2, checks
   1 and 2). That outpoint moves on *any* holder's transfer of the asset, and
   `sig_all_hash` commits to it, so a pre-signed exit is invalidated by a
   stranger's unrelated transaction. The window is not small; it is nil in
   practice.
3. **The issuer cannot move it either.** An OpenDAMP asset has no clawback leaf.
   The issuer's powers are a policy update and a halt -- freeze a key, blacklist
   an outpoint -- and neither delivers a coin to somebody else. The Tier C
   escape hatch simply does not exist here.

Together those say that on default, no party has a path to the collateral:
not the covenant, not a pre-signature, not the issuer. Locking it is easy --
a whitelist entry with `send_after` set to the maturity height freezes the
borrower's key by consensus, which is a *better* lock than Tier C's -- but a
lock nobody can ever open in the lender's favour is a trap for both sides, not
collateral.

What does work is a different instrument, and it is worth naming rather than
pretending otherwise: a **repurchase**. The borrower sells the collateral to the
lender outright -- an ordinary OpenDAMP transfer to `C_U(lender)`, which needs no
new machinery at all -- and the lender's obligation to sell it back is secured by
a Tier A covenant vault holding a bond. The bond need only cover the borrower's
*equity*, collateral value minus debt, because the debt offsets the rest; a
lender who never returns the collateral forfeits the bond, which leaves the
borrower exactly as well off as if the loan had been liquidated at par.

That inverts who holds what, and Pignus says so in those words. It is not
collateralized lending and must never be shown as if it were: the borrower has
sold their asset and holds a claim. It is also how securities financing actually
works, which is the point -- the regulated-asset tier ends up with the
regulated-market instrument. Section 8.1 specifies it.

### 8.1 The repurchase, specified

A repurchase has two legs, created together and settled together.

**Leg one, off the covenant.** The borrower transfers `q` units of the OpenDAMP
asset to `C_U(lender)` by an ordinary transfer. Nothing here is special: it is
the transfer the asset already supports, subject to the whitelist the issuer
already publishes, and the lender must be an approved holder for it to confirm
at all -- which is the right gate, because a lender who could not lawfully hold
the asset has no business financing it. The lender separately pays the borrower
`principal` in the debt asset.

**Leg two, on the covenant.** The lender funds a two-leaf vault with `bond` of
the debt asset, where

    bond = collateral_value_at_origination - debt

in debt-asset atoms: the borrower's equity. The vault is built from the same
leaf builders section 2 uses, with no change to any of them, but it is NOT the
four-leaf `vault_taptree` -- the two leaves are parameterised independently,
which is exactly what the section 2 vault cannot do because it passes one payout
program to both REPAY and RECOVER.

    RETURN  = build_repay_leaf(asset_c   = the debt asset,
                               asset_d   = the OpenDAMP asset,
                               debt      = q,
                               lender_prog   = C_U(borrower),
                               borrower_prog = the lender's payout)

    FORFEIT = build_recover_leaf(recover_after = forfeit_after,
                                 asset_c       = the debt asset,
                                 lender_prog   = the borrower's payout)

`C_U(borrower)` is a P2TR, so it is a 32-byte version-1 payout program and the
covenant pins it like any other. RETURN therefore says, in the covenant and
without an oracle: *the bond is released to the lender only in a transaction
that delivers `q` units of the OpenDAMP asset to the borrower's own C_U
address.* FORFEIT says: *after `forfeit_after`, anyone may sweep the bond, and
only to the borrower.*

**Settlement is one atomic transaction.** The borrower's payment of `debt` is
NOT covenant-checked -- the two output slots RETURN inspects are already spent
on the collateral and the bond -- so it must never be made outside the RETURN
transaction. It does not need to be checked, because both parties must sign that
transaction anyway: the lender signs their `C_U(lender)` input, the borrower
signs the input funding the debt, and neither signs a transaction missing what
they are owed. The full shape:

| # | Input | | # | Output |
|---|---|---|---|---|
| 0 | the OpenDAMP verifier output | | 0 | the verifier output, returned |
| 1 | `C_U(lender)`, holding q | | 1 | q to `C_U(borrower)` |
| 2 | the bond vault | | 2 | the bond, to the lender |
| 3 | the borrower's debt-asset UTXO | | 3 | `debt`, to the lender |
| | | | 4 | the borrower's change |
| | | | 5 | the fee |

That is four inputs and six outputs, which is **exactly** OpenDAMP's
`N_max_inputs` and `N_max_outputs`. There is no spare slot in either direction.
Two consequences the platform must enforce, because nothing else will:

- The borrower's debt-asset side must be a **single** UTXO. Pignus consolidates
  it first if it is not, in a separate transaction, before composing settlement.
- The **fee is paid in the debt asset**, out of that same input. A separate fee
  input would not fit. Sequentia's any-asset fees make this ordinary rather than
  a workaround, and OpenDAMP's fee delta (opendamp-design.md 2.3) is what lets
  the verifier tolerate the fee output at all.

**What each outcome pays.** Write `V` for the collateral's value at origination,
so `bond = V - debt`.

| Outcome | Borrower ends with | Lender ends with |
|---|---|---|
| Settled | the collateral, minus `debt - principal` | `debt - principal` |
| Borrower never pays | `principal + bond = V - (debt - principal)` | the collateral, having paid out `V - (debt - principal)` |
| Lender never returns | `principal + bond`, having paid nothing | the collateral, less the bond |

The interest `debt - principal` is what the borrower pays in every branch and
what the lender earns in every branch, which is the property that makes the
instrument sound: neither party gains by failing to perform. A borrower who
simply walks away after `forfeit_after` sweeps the bond themselves and is left
exactly where a liquidation at the origination price would have left them.

**What it does not protect against.** The bond is fixed at origination, so the
borrower is made whole at the price the deal was struck at, not at the price on
the day. If the collateral appreciates and the lender declines to return it, the
borrower keeps `principal + bond` and loses the upside. That is the borrower's
residual exposure, it is not fixable without a price feed for a private
restricted asset, and Pignus states it in those words on the confirmation
screen. Where such a feed does exist, the parties may instead use a
three-leaf variant adding `build_liquidate_leaf`, and the platform offers it
only when a feed is configured for that asset.

**Naming.** The product is labelled **repurchase**, never "loan", in the book, in
the offer list and on the confirmation, and the borrower's confirmation says in
one sentence that they are selling the asset and holding a claim. It is not
collateralized lending and must never be shown as if it were. It is also how
securities financing actually works, which is the point: the regulated-asset
tier ends up with the regulated-market instrument.

## 9. Where the code lives

The platform lives in this repository, `pignus`, alongside the other
Sequentia sub-projects; the covenant and its consensus-level proof stay in the
node repository, the same way SeqOB's covenant ships with the node while the
daemon driving it ships separately.

What runs where:

- **the browser**, at `/lending/` -- composes every transaction, derives every
  address, and sends only a signature request to the wallet extension. It is
  the only place a second implementation of the covenant exists (`web/pignus.js`
  and `web/offer.js`), because a browser cannot import the Python one; both are
  pinned byte for byte to the golden vectors and the page refuses to run at all
  if that pinning fails.
- **the CLI**, on the downloads page -- the same operations against your own
  node, for anyone who would rather not use a website.
- **the oracle and the loan book** -- services on the testnet server. Neither
  can move anything: the book is discovery, and the oracle asserts a number.

Two things were found only by building the browser client, and both are worth
recording because neither was visible from the library side: payouts that could
only be taproot (section 2.6), and exits that required a signature the extension
cannot make (section 2.5). A design that is only ever exercised by its own test
suite hides exactly this class of defect.
