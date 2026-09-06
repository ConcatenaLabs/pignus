# Pignus: non-custodial collateralised lending on Sequentia

The loan-vault covenant (section 2) lives in the node repository's
[`test/functional/pignus_covenant.py`](https://github.com/ConcatenaLabs/Sequentia/blob/master/test/functional/pignus_covenant.py),
where it is proven against a node by `feature_pignus_vault.py`,
`feature_pignus_oracle_set.py`, `feature_pignus_offer.py`,
`feature_pignus_hashlock.py` and `feature_pignus_attack.py`. Everything that
drives it -- the oracle, the loan book, the watcher, the browser client and the
Bitcoin-collateral construction (section 7) -- lives in the `pignus` repository,
and runs on the testnet at
[sequentiatestnet.com/lending/](https://sequentiatestnet.com/lending/). The
OpenDAMP repurchase (8.1) is there too: the bond vault, the verification of both
legs, the forfeit path, and the four-input settlement `pignus-cli repo-settle`
composes and attaches the covenant witness to. Two of that settlement's inputs
are Simplicity spends of the issuer's covenants, and nothing in this repository
signs them, so a settlement cannot yet be broadcast; section 8.1 says what that
means for a holder.

*Pignus* is the Roman-law term for property pledged as security for a debt: the
creditor holds the pledge, the debt is owed separately, and redeeming the debt
redeems the pledge. That is exactly the shape of the thing.

Companion documents, all in other repositories:
[`openamp-design.md`](https://github.com/ConcatenaLabs/Sequentia/blob/master/doc/sequentia/openamp-design.md) and
[`opendamp-design.md`](https://github.com/ConcatenaLabs/Sequentia/blob/master/doc/sequentia/opendamp-design.md) in the node
repository (the two restricted-asset models this coexists with, section 8),
[`simplicity-dex-covenant-offers-design.md`](https://github.com/ConcatenaLabs/seqdex/blob/main/docs/simplicity-dex-covenant-offers-design.md)
in the `seqdex` repository (the covenant-offer design this borrows its
output-map and self-replication techniques from), and
[`03-bitcoin-anchoring.md`](https://github.com/ConcatenaLabs/Sequentia/blob/master/doc/sequentia/03-bitcoin-anchoring.md) in
the node repository (why section 6.4 exists at all).

## 1. What is being claimed

A borrower locks collateral in a single taproot UTXO and receives principal in
the loan's debt asset -- any accepted Sequentia asset, since none is privileged
here. The vault's spending rules are the loan agreement, compiled. Precisely:

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
[`test/functional/pignus_covenant.py`](https://github.com/ConcatenaLabs/Sequentia/blob/master/test/functional/pignus_covenant.py)
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
**at least** `L - seize` to the borrower. The inequality is deliberate and is
not a rounding allowance: a liquidator may always give the borrower more than
the covenant demands, never less, so the leaf constrains the only direction
that can hurt. It has a practical use as well as a theoretical one. `IsDust`
applies to outputs in a transaction's fee asset, so a seizure just past the
threshold, paid for in the collateral asset, leaves the borrower a return the
relay would refuse — and the composer lifts that return to the dust threshold
out of the seizure rather than building a transaction no node will take.

The liquidator keeps what is left, whose value at the
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

RECOVER pins its destination rather than demanding the lender's signature, and
that is the better design on its own terms. A browser wallet extension signs its
own transaction inputs but cannot sign a covenant leaf, and exposes no x-only
key to bake into one, so a signature-gated backstop would be one that a lender
using a browser could never execute -- not a backstop but a trap holding their
collateral. With the destination pinned the lender needs no key beyond the
address they are paid at, so there is nothing to lose, and need not be online,
for the same reason REPAY is permissionless. Letting anyone trigger it is safe
because it can only ever pay the lender.

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

Measured leaf sizes for a real loan: REPAY 192 bytes, LIQUIDATE 352,
DEFAULT 346, RECOVER 98. DEFAULT and RECOVER vary by a byte with the size of
the locktime they carry, so those are the figures for an ordinary height.
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

**Offers.** A lender's offer is FUNDED: the principal already rests in an offer
covenant, and any borrower may take it unilaterally, in one transaction that
locks a correctly shaped vault. The lender need not be online, and the book has
a coin it can check rather than a promise it cannot. That covenant is in the node
repository's
[`test/functional/pignus_offer.py`](https://github.com/ConcatenaLabs/Sequentia/blob/master/test/functional/pignus_offer.py),
proven by `feature_pignus_offer.py` and driven by `pignus/offers.py` and
`web/offer.js`; it recomputes the vault's taproot address from a
witness-supplied borrower key with `OP_TWEAKVERIFY` plus the tagged hashes, the
technique OpenAMP's containment covenant proved.

A *signed* offer -- one the lender must be online to co-sign at take time -- is
strictly worse and does not exist for issued-asset collateral: the book refuses
any other kind, and publishes one only after checking the coin at the offer's
address on chain. (A cross-chain offer, section 7.4, is necessarily a signed
promise with no coin behind it, because there is no covenant on Bitcoin to
fund.) The book itself is discovery. It holds no funds, and an altered record
would compile to a different address, which is what the borrower's
reconstruction catches; but a funded offer's record is the only copy of the
terms that can ever spend the offer's coin, so a delisted offer is kept, hidden,
until it is withdrawn.

The take is one atomic transaction:

    inputs   the offer's coin, borrower's collateral utxo(s)
    outputs  the vault (L of C at the covenant address)          <- 2k
             the offer's remainder, if it held more than one lot <- 2k+1
             the principal (the loan amount of D) to the borrower
             +  changes, and the network fee output

The first two positions are not a convention: with the offer's coin at input
`k`, the covenant reads output `2k` as the vault and output `2k+1` as the
remainder claimed back to the offer's own address. Putting the borrower's
principal at `2k+1` is the obvious mistake and the leaf refuses it. When the
whole offer is drawn there is no remainder, and output `2k+1` must then be one
that does NOT carry the debt asset -- the network fee output serves.

The borrower MUST, before signing, reconstruct the vault address from the terms
and check it equals the vault output's scriptPubKey, and check the internal key
is NUMS. That single check is what makes everything in section 1 true; a wallet
that skips it has silently reintroduced a trusted party. `pignus-cli verify` and
the page both do it, and the page never asks a wallet to sign a vault it did not
reconstruct locally.

A direct origination -- both parties signing one transaction that funds the
vault and pays the principal, with no offer covenant in between -- is the same
shape, and `build_origination` in `pignus/vault.py` composes it. Either party
can walk away before signing and neither is ever exposed to the other; it is the
same PSET co-signing flow SeqDEX already uses for same-chain atomic swaps, so it
is proven machinery rather than new. It is nevertheless a library and nothing
more: no command, page flow or book entry uses it, because a funded offer does
the same job without needing the lender at the keyboard.

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
adds a signer and a publication endpoint and no second price pipeline; what it
adds beyond the signature is judgement about when not to sign. An attestation
is stamped with the time the price was observed -- the feed's own
`_meta.updated` where it publishes one, else the fetch -- rather than the
signing time, and a snapshot that has not changed is not signed again, so a
feed that stops answering yields nothing that looks fresh. A price that moves
further than `max_jump` from the last signed one in a single step is not
signed until it has stayed there for `jump_rounds` consecutive rounds, because
a feed that switched units is a perfectly good signature over a number a
thousandfold off; a board on which every market comes back byte-identical for
`flat_rounds` is called frozen and left unsigned; a clock more than sixty
seconds from the feed's is said in the journal and in `/healthz`, since a book
refuses an attestation from the future or already stale. The feed is read only
over https unless it is local or the operator says otherwise, and a redirect
or an answer over a megabyte is refused: the oracle would otherwise sign
whatever a path in between rewrote.

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

Two mitigations, in increasing cost.

### 5.1 Re-covenanting on top-up

A borrower adding collateral moves to a fresh vault with a later `not_before`,
which retires every attestation older than the top-up. This is the practical
answer and it falls out of the design for free: a top-up is a REPAY-and-reopen,
or an explicit new origination.

### 5.2 Epoch commitment

The oracle signs `feed_id || timestamp || price`, and nothing here reads an
epoch. An epoch scheme -- the oracle signing `feed_id || epoch || price` with
`epoch` advancing every N minutes, and the vault baking a `min_epoch` -- would
not remove the window; it would only bound how far back a saved attestation can
reach, and only while `min_epoch` advances, which requires re-covenanting. It
would help mainly short-term loans, where `not_before` can be set close to
origination, and that is why this design leaves it out.

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

Hardening comes in two shapes. The first is open to any operator willing to put
a threshold signer behind the oracle's key; the second is in the covenant.

**A threshold group key** (FROST, the `PolicySigner` seam OpenAMP already built)
would have the vault name ONE key, with the m-of-n living in the signing
protocol and nothing on chain changing. That is the cheapest shape, and the
group key's invariance under resharing means the signer set can rotate without
moving a single vault address. Its cost is that the signers must run a joint
protocol -- there is a coordinator, and the signers' liveness is coupled.
Pignus's oracle daemon signs alone from one keyfile, so an operator who wants
this brings their own signer. That key is made only on a fresh install, where
no attestation log exists yet, and its creation is announced; a machine
holding the log but not the key is a lost key, and the daemon refuses to start
until the key is restored or `--create-key` says a new one is meant, because a
new key signs for nothing the live loans under the old one will accept.

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

Two properties fall out and are worth stating, and the second has a condition
on it. A single compromised oracle cannot trigger a liquidation, because it
cannot reach the threshold alone -- and a signature from one key replayed into
another key's slot fails, since every slot pins its own key. That holds for any
m-of-n. A single dead oracle cannot BLOCK a liquidation either, but only where
m is less than n: at n-of-n every slot is required, so any one outage stops
every liquidation under that set until it comes back.

Which is the choice a lender is making, and `--oracle-threshold` defaults to
n-of-n deliberately, for a set the lender typed: it is the setting under which
no single oracle outage can liquidate, and the price of that is the setting
under which one can stall. A set taken from the book (`--oracles book`) has a
size the lender did not type, so there the threshold must be said, and the
command refuses to let n-of-n happen by default. A
lender who would rather a liquidation survive an outage sets m below n and
accepts that m keys agreeing is enough. Neither is the safe answer; they are
opposite risks, and the default takes the one where nobody's collateral moves
by mistake.

An abstaining slot carries an EMPTY signature, which
`OP_CHECKSIGFROMSTACK` treats as false; a non-empty invalid signature aborts the
script instead, so a slot cannot be stuffed with rubbish to fake an abstention.

One trap the builder refuses outright: a 1-of-n set is weaker than a single
oracle, not stronger, because ANY of the n can act alone. `sanity_check()` says
so, and duplicate keys are rejected because one signer filling two slots makes
the threshold mean less than it says.

**Keys are pinned, retired and disowned in the open.** An oracle publishes at
`/v1/pubkey` the keys it signed with before (`previous_keys`) and any of its
own it declares compromised (`compromised_keys`). A book pins the key it
expects from each oracle it quotes (`oracle_keys`): a served key that differs
is reported and its attestations are ignored, so a lost-and-recreated key or
one substituted in transit prices nothing. It accepts no attestation under a
key its oracle has declared compromised -- a declaration counts only for the
declarer's own keys, present and previous, so no oracle can disown another --
and marks every loan and offer whose vault bakes such a key
`oracle_compromised`. It serves the previous keys at `/v1/oracles`, so a loan
baked to one reads as a rotation rather than a stranger's. It also checks
every quoted oracle's precisions against the registry's for each market and
drops an attestation whose precisions disagree, since a price off by a power
of ten carries a valid signature. Under an n-of-n set the effect of any of
these on one oracle is the one described above: the book withholds the loan's
price, health and `liquidatable` until every oracle is back.

### 6.2 What the platform can do

For issued-asset collateral, nothing: the book is discovery, the watcher is
read-only, and the liquidator bot is just the first participant to notice --
anyone can run one, and the covenant does not care which one wins. The
cross-chain relay is the exception, and section 7.4 bounds it: it cannot forge
a message, but it holds handshake material it could lose or withhold, it frees
a lender's lot from a take that stalls on its own clock, and it refuses offers
whose deadlines or fees fail the rules every party checks.

### 6.3 Liquidation races

Liquidation is a permissionless race, and the winner is whoever gets a valid
spend mined. This is an unpriced race, not a theft: every racer must pay the
lender in full and return the surplus, so the borrower and lender are
indifferent to who wins. The bonus is what prices the race.

### 6.4 Bitcoin anchoring

Sequentia reorgs when Bitcoin reorgs, in real time, and that outranks
everything. So a vault funding transaction can be undone by an anchor-driven
reorg, exactly as a covenant CLOB order can. The watcher therefore classifies a
vault whose funding has been reorged away as GHOST, keeps it out of every LIVE
view, and goes on watching it -- the funding transaction is still valid and is
normally mined again, and the vault returns to UNCONFIRMED or LIVE when it is.
A lender must not treat a loan as originated until its funding is buried by the
depth their risk appetite justifies. This is not a Pignus-specific caveat; it is
the chain's first principle, and any design that pretended otherwise would be
wrong.

Only a watched outpoint can be seen to come back, which is why a ghost is
re-tracked across a restart rather than forgotten, and why the funding height
and block hash are persisted with it: they are what tells an anchor-driven reorg
from a spend the watcher simply could not reach.

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

- leaf RECLAIM: `SHA256 <h> EQUALVERIFY <P_borrower> CHECKSIGVERIFY
  <P_lender> CHECKSIG` -- both parties and the secret, the repayment path;
- leaf SEIZE: `<P_lender> CHECKSIGVERIFY <P_oracle> CHECKSIG` -- lender and
  oracle jointly, the liquidation path;
- leaf TIMEOUT: `<recover_after> CLTV DROP <P_lender> CHECKSIG` -- the backstop.

Repayment is linked to the BTC release by ONE HASH, which appears in both
chains' scripts:

1. The lender picks a secret `t` and publishes `h = SHA256(t)`.
2. At origination the lender hands the borrower their half of the RECLAIM
   signature on the transaction that returns the BTC -- an ordinary BIP340
   signature the borrower verifies on the spot. RECLAIM also demands `t`, so
   the borrower holds a release they cannot yet use.
3. The borrower repays on Sequentia into a hashlocked output whose CLAIM leaf
   pays the lender against `t` and whose REFUND leaf returns the money to the
   borrower after `repay_deadline`.
4. The lender claims the repayment, which publishes `t` on the Sequentia chain.
5. The borrower reads `t` off the chain and spends RECLAIM with it, the
   lender's release and their own signature.

**The link is a hash rather than an adaptor signature, and that is the point.**
An adaptor signature under a point `T = t·G` would make step 2 a signature the
borrower cannot complete until `t` is public, which sounds like the same thing.
It is not, because it asks the borrower to believe that the `h` baked into their
repayment output and the `T` their release is encrypted under came from one
secret. Nothing available here checks that: proving `SHA256(t) = h` and
`t·G = T` together needs a proof this protocol has no way to carry, and a lender
who published an unrelated `h` would take the repayment AND keep the collateral,
with no collusion and no race. With one hash in both scripts there is nothing
left to assert and nothing left to take on trust -- the borrower compiles both
scripts and reads the same 32 bytes out of each.

If the lender never claims, the borrower recovers the repayment after the CLTV
and the lender takes the BTC via TIMEOUT: the loan unwinds and neither side is
robbed, though the stall is not free for the borrower -- see the exposure table
below.

The claimant on the Bitcoin side must re-run the anchor-safety check on the
Sequentia reveal before acting on `t`, for the reason in section 6.4 -- a
covenant cannot introspect anchoring, so this stays a watcher discipline. This
is the same discipline the SeqDEX cross-chain leg already documents.

**Origination is atomic, because otherwise it is a gift.** Steps 1 to 5 describe
a loan that already exists. Getting into one is the harder half, and the obvious
sequence -- borrower funds the vault, lender then sends the principal -- gives
the lender a free option: say nothing, wait for `recover_after`, and sweep
collateral that was never paid for. No amount of care on the borrower's side
detects it in advance. So the collateral does not go into the vault first.

The borrower funds a **pre-vault**, a P2TR output with the NUMS internal key and
two leaves, where `w` is a secret the borrower chooses and `H_w = SHA256(w)`:

- leaf UPGRADE: `SHA256 <H_w> EQUALVERIFY <P_borrower> CHECKSIGVERIFY
  <P_lender> CHECKSIG` -- moves the collateral into the vault, and needs both
  parties: a borrower who could move it alone would take the principal and walk
  off with the collateral, and a lender who could move it without `w` would
  start a loan they had not paid for;
- leaf ABORT: `<abort_after> CLTV DROP <P_borrower> CHECKSIG` -- takes it back.

The borrower signs the single UPGRADE transaction, pre-vault to vault, at
origination, before anything is broadcast. That fixes the vault's outpoint,
which is what the lender's release has to commit to, and it means the lender can
start the loan the moment `w` is public -- without the borrower being online.
The pre-vault holds `btc_amount + upgrade_fee`, because after origination the
borrower may be gone and the move still has to pay for itself.

It also imposes an order. The vault's address commits to `h`, so the borrower
cannot sign the move into it until the lender has drawn the secret: the
handshake is ask, hash, pre-sign, release, fund. Every step before the last
commits nothing, so a borrower who stops at any of them has lost nothing but the
time.

The principal is paid into a hashlocked Sequentia output of the same shape as
the repayment: CLAIM pays the borrower against `w`, REFUND returns it to the
lender after `d_refund`. Both Sequentia outputs use the covenant's
`build_hashlock_leaf`, so **neither leg needs a signature** and neither can pay
anyone but the party its address already names. The leaf also pins the
preimage's size -- `OP_SIZE 32 OP_EQUALVERIFY` before the hash -- because
OP_SHA256 will hash a stack item of any length, and a secret of any other
length would be paid here and then be useless on the chain it has to cross to:
the party waiting there looks for 32 bytes, and a long preimage makes the
Bitcoin spend it unlocks non-standard. That is what lets a browser
drive the whole thing: the wallet extension signs its own inputs and a Bitcoin
sighash, but no Sequentia covenant leaf, and here it does not have to.

Origination therefore runs:

| # | who | what | if it stops here |
|---|---|---|---|
| 1 | borrower | prepares the pre-vault funding, unbroadcast, and asks for a loan | nothing has happened |
| 2 | lender | draws this loan's secret and publishes `h` | nothing has happened |
| 3 | borrower | can now derive the vault, and pre-signs UPGRADE | nothing has happened |
| 4 | lender | verifies that pre-signature and signs RECLAIM | nothing has happened |
| 5 | borrower | verifies the release, broadcasts the pre-vault funding | borrower ABORTs at `abort_after`, out only a fee |
| 6 | lender | sees the collateral confirmed, pays the principal into the hashlock | lender REFUNDs at `d_refund`; borrower ABORTs |
| 7 | borrower | claims the principal, publishing `w` | lender REFUNDs at `d_refund`; borrower ABORTs |
| 8 | lender | reads `w`, waits for the claim to be anchor-safe, broadcasts UPGRADE | **the lender loses**: the borrower has the principal and, at `abort_after`, the collateral |

Only the last step exposes anyone to a loss rather than to a delay, and only the
lender, who controls whether they are online. The margin between `d_refund` and
`abort_after` is what makes that exposure a choice rather than a race, which is
why `timelocks_sane()` refuses a loan whose deadlines do not leave it: at least a
day, in wall-clock seconds, converting each chain's heights at its own block
time.

The fee on that last step is the other half of the same exposure, and it is
worth stating rather than leaving to be discovered. The borrower signs the move
into the vault at origination, so its fee is fixed before anyone knows what the
parent chain will cost. It cannot be replaced -- the signature commits to the
whole transaction -- and its only output is the vault, which nobody can spend
to pay for it, so it cannot be bumped from either end either. A fee too low to
confirm before `abort_after` is a loan the lender has paid for and cannot
start. That is why the pre-vault's fee is PRICED FROM A BITCOIN NODE when an
offer is published rather than left at a constant -- a constant is a fee that
was right when the parent chain was quiet -- why `timelocks_sane`, which every
party calls, refuses a loan whose upgrade fee is under a flat
10,000 satoshis whatever the chain says, why a taker refuses an offer whose
fixed fee has fallen far behind what the chain now wants, and why the
margin above is measured in days rather than hours. The same function refuses a
loan whose `recover_after` does not sit a day past `repay_deadline`, because a
lender who could claim a repayment after the Bitcoin timeout had opened would
be racing the borrower for the collateral with the repayment already in hand.

The four deadlines are absolute block heights, judged against both chains'
tips by everyone who handles them, all by the same rules: `d_refund` at least
two hours ahead, `abort_after` at least a day after `d_refund`,
`repay_deadline` at least a day after `d_refund` plus the two-hour claim
margin, `recover_after` at least a day after `repay_deadline` and past
`abort_after`. `btc-offer-publish` judges them against the tips the book
reports and refuses an offer that fails; the relay judges them again at
`POST /v1/btc/offers` and refuses too, and records a warning on a take whose
deadlines have drifted -- a warning rather than a refusal, since the offer was
already admitted; a taker refuses at take time; and the responder judges them
before it reserves a hash, before it signs and before it pays. A relay whose
Bitcoin node is not answering cannot judge them at all, says so in
`/healthz`, and admits the offer unchecked, which is why the other checks
exist. Because the heights are absolute, the offer leaves the board two hours
before `d_refund`, and a take made later has a shorter term than an earlier
one.

**The written repayment deadline is not the one a borrower is held to.**
Claiming a repayment publishes the secret, so a lender who claimed as the
borrower's own refund opened would hand them the debt AND the collateral: they
would refund the repayment and reclaim with the secret. So a lender stops
claiming `CLAIM_MARGIN_BLOCKS` -- two hours of Sequentia blocks -- before
`repay_deadline`, and the refusal is permanent, because height only rises. A
repayment made inside that window is one nobody will answer: it comes back to
the borrower at `repay_deadline`, and their collateral is swept at
`recover_after` with the debt paid and refunded.

That makes `repay_deadline - CLAIM_MARGIN_BLOCKS` the EFFECTIVE deadline, and
it is what every tool quotes: `timelocks_sane` measures the repayment window
and the minimum term against it, and the margin to `recover_after` from the
written figure, which is the stricter of the two; the offer row and the loan
row on the page show it with the written figure beside it, and
`effective_repay_deadline` is the one place it is computed.
`web/btcborrow.js` carries the same constant, and a test compares the two,
because a borrower told a deadline nobody honours is the whole of this
paragraph going wrong.

**What each party is exposed to, once a loan is live.** The bounded losses are
worth stating plainly rather than being left as "nobody is robbed":

| outcome | borrower ends with | lender ends with |
|---|---|---|
| borrower repays, lender claims | the collateral, less the interest | the debt |
| borrower repays, lender stalls past `repay_deadline` | the repayment back, less two fees; the collateral is lost at TIMEOUT | the collateral, worth more than the debt on an over-collateralised loan |
| borrower never repays | nothing | the collateral, at TIMEOUT |
| price crosses the strike | the collateral, less what SEIZE took | the debt, if the oracle co-signs |
| the lender LOSES their key | the repayment back at its own refund leaf; the collateral is stuck in the vault for ever | nothing: they can neither claim nor sweep |

The second row is the honest one: a lender who stalls gives up the repayment and
takes collateral worth more, so on an over-collateralised loan stalling **pays**.
The last row is the one nobody plans for: a lost lender key is not a loss
confined to the lender, because all three of the vault's leaves need it, and there
is no timeout that opens for anybody else. It is why `deploy/DEPLOY.md` treats
that key as the money it is.
The borrower's protection is not the lender's incentive; it is the margin
`timelocks_sane()` enforces plus the borrower's own duty to repay early enough
that the claim is buried well before `recover_after`. A borrower who repays at
the last block would be choosing the risk, and the page does not let them:
until the effective deadline it names the Sequentia block to repay before,
beside the button, and past it there is no button -- the row, the alert and
the dialog say a repayment would not be claimed and would release no
collateral, and the page refuses to build one. It also asks the book whether
the collateral is still in the vault before composing a repayment, refuses one
for a vault the lender and the oracle have seized or the lender has swept, and
says in the dialog when the book could not tell.

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
- **Settlement at maturity** -- the shape that fits is a DLC, because there the
  outcome set is one-dimensional and the date is known. The oracle announces one
  public event, the BTC/USDX price at the maturity date, and attests it without
  knowing any loan exists; each side adaptor-signs one contract-execution
  transaction per price bucket, and the attestation is the decryption key for
  exactly one of them, which removes the oracle's per-loan involvement at
  maturity entirely. `pignus/dlc.py` implements those primitives -- the
  announcement, the price buckets, the CETs and the attestation -- and proves
  them on a rig. It is not wired into the oracle daemon, the CLI or the page: a
  live cross-chain loan settles at maturity through TIMEOUT, to the lender.

Stating it plainly: BTC collateral is the one tier where the oracle is trusted
*interactively* rather than only for a number. That is the price of collateral
on a chain with no covenants, and it is why the unrestricted-asset tier is the
one to use where a choice exists.

### 7.3 What the borrower trusts

Section 2's tier asks a borrower to trust an oracle for one number. This one
asks for more, and the list is short enough to state:

- **The lender, to claim the repayment.** Repayment and release are bound, but
  nothing forces the lender to pick up the money. The exposure table above says
  what stalling costs and what it pays.
- **The lender, to be reachable at origination.** A lender who signs the release
  and then vanishes before the collateral is upgraded costs themselves, not the
  borrower -- but a lender who never signs at all costs the borrower the
  pre-vault fee and a wait until `abort_after`.
- **The oracle named in the offer.** SEIZE is a plain 2-of-2 with no covenant to
  refuse it, so the oracle's signature *is* the liquidation decision. A borrower
  should check the offer's `oracle_x` against the set the book publishes, and
  refuse an offer whose oracle is the lender's own key -- which the tools also
  refuse to build. No script constrains the decision, so the oracle constrains
  itself before signing and publishes afterwards. It co-signs only a sighash it
  rebuilt from the loan's own terms, only for a loan that names its key, only
  against an attestation of its own that is recent (`--max-age`, 600 seconds
  by default) and whose price is strictly under the strike at the loan's own
  `price_scale`, only while its own daemon still stands behind that market's
  price (a market its `/healthz` reports stale or in error is refused), never
  with a key it has itself declared compromised, and, given a `book` in its
  configuration, only for a take that book still lists under this oracle's key
  as signed, disbursed or live -- a loan that has been repaid or aborted is
  not one to seize. Every co-signature it does make is published at
  `/v1/seizures` beside the attestation that justified it, so the decision is
  checkable afterwards as well. What the oracle judges by is the
  strike, and the strike is in no Bitcoin script: the lender's signature over
  the offer would pin it only against a stranger, since the lender is the party
  asking for the seizure and can sign the same loan again over any strike. So a
  take carries the borrower's own signature over the id of the offer as it was
  when they took it, and an oracle refuses a seizure request whose terms no
  longer hash to that id. The strike a seizure is judged against is therefore
  one the borrower agreed to, not one the lender presents.
- **The deadline ordering.** `timelocks_sane()` refuses a loan whose deadlines
  do not leave the margins section 7.1 describes, but a borrower building terms
  by hand should read them: the gap between `d_refund` and `abort_after`, and
  between `repay_deadline` and `recover_after`, is what makes each exposure a
  choice rather than a race.

Nothing in the list is the relay, and nothing in it is Pignus.

### 7.4 Origination through the book

Two people who already know each other exchange a ticket file by hand. To be
found by strangers, a lender publishes an offer on a `pignusd`, which carries
messages between the parties: `/v1/btc/offers` (the lender's offers, judged
for their deadlines, a fee floor and an oracle that is not the lender before
they are stored; a lender withdraws one at `/withdraw` and re-signs one at
`/resign`), `/v1/btc/take` (a borrower asking for a loan, with a reclaim fee
inside the floor and cap the book prices from the parent chain),
`/v1/btc/hash` (the lender
drawing this loan's secret), `/v1/btc/presig` (the borrower signing the move
into the vault that hash implies), `/v1/btc/adaptor` (the release
coming back) and the lender's reports that the principal is paid, the loan is
started, the repayment is claimed or an unclaimed principal is taken back. The
borrower reports two things of their own -- where they took the principal and
where they paid the debt -- and those record an outpoint and change no status at
all. That asymmetry is the point. A status a borrower can set is a status they
can use: one that moved a take out from under the step their lender was about to
take would leave the principal paid and the collateral never vaulted, and it
would pin a lot of the lender's offer in a state nothing else clears. So a
responder decides what to do from its OWN state file and the two chains, never
from a status the relay is holding, and the borrower's reports only ever save it
a scan. They are signed all the same, because an unsigned hint is a way to write
into somebody else's loan. `docs/api.md` documents each of them.

The relay holds no key and can move nothing, and it is never believed. Every
message a responder will act on carries a BIP340 signature by the key the loan
already names, over a tagged hash of exactly the fields that matter; the relay
verifies before storing and the responder verifies again before acting. An
unsigned offer would let anyone publish in a lender's name and have that
lender's own responder pay it out, which is the attack the signatures exist for.
The relay also recomputes rather than accepts: it rebuilds every address from
the offer's own terms plus the four things a taker chooses, checks the
borrower's advance signature moves that collateral into that vault, and checks
the lender's release against the vault it derived -- before storing either. And
a lender's responder repeats all of it, because the relay is a message carrier,
not an authority: it rebuilds each loan from the offer IT signed rather than
from the take the relay hands back.

Each take draws its **own** secret `t`. One secret shared across an offer's
takes would let any borrower's repayment release every other borrower's
collateral.

The lender's half runs as a process (`pignus-cli btc-respond`), because the
lender draws secrets and drawing a secret in a browser means storing it in one.
It keeps a state file recording what it has already done, and around that
file the things that stop a principal being paid twice across a crash: one
responder per key, held by a lock beside the file; a refusal to start on a
state file it cannot read, parse or write; an in-flight mark written before a
payment goes out and cleared only by the payment being found on chain or by
an operator running `btc-responder-clear`; and a payment that fails before it
is broadcast -- a wallet that is not loaded, cannot fund or cannot sign --
clearing the mark itself, since nothing went out. The responder refuses to
start when it cannot reach the Sequentia wallet its configuration names or
without RPC to both chains, will not sign a release that wallet could not pay
or one past the offer's lot count, and judges the loan's deadlines against
both tips before every step that commits it. Bitcoin confirmations on the
collateral before a principal is paid (`--disburse-conf`) have a floor of
two, the shortest depth that survives a one-block reorg. Every refusal is
recorded on the take as a dated wait, where `btc-responder-status` and the
timer see it, rather than as a journal line; a wait on a take the relay has
since ended is cleared, so a routine refusal is not a standing alarm. That
process is the one component in
this tier whose absence costs the other party: a borrower whose take is never
signed waits and aborts, and a borrower whose collateral is never upgraded keeps
the principal.

The borrower's half runs in the page, and the page believes the chains before
it believes the relay. On every load it reads three facts straight off them
rather than off the lender's reports: whether the collateral is in the vault
(the vault outpoint on Bitcoin, through the book), whether the lender has
claimed the repayment (the secret in the witness that spent the repayment
output, through `/v1/spend`), and whether the funding this browser signed
ever reached the chain. So a report the responder never sent costs the
borrower nothing: the abort button goes when the loan is live, the reclaim
opens when the secret is in a witness, and a take whose funding is missing
from the chain twice running says so. The signed funding is remembered before
it is broadcast and can be sent later, after the release and both chains'
deadlines are checked again; a take the lender never answered has a button to
forget it. An abort pays only to the address the lender's release was signed
over -- the page rebuilds the RECLAIM sighash from the terms and verifies the
release against it before signing -- and pays the parent chain's fee of the
day where that is more than the one fixed at the take.

## 8. Collateral tiers

| Tier | Assets | Enforcement | Trust |
|---|---|---|---|
| A | any unrestricted issued asset, the Sequence token (tSEQ) included | The section-2 covenant | Oracle, for one number |
| B | Native BTC | Section 7, cross-chain | Oracle, interactively, for liquidation; the lender, for claiming the repayment and for being reachable at origination |
| C | OpenAMP (`cosign`) assets | A pledge the policy server enforces | The issuer's policy server, and before maturity the borrower's consent |
| D | OpenDAMP (`damp`) assets | Not lending: a repurchase (8.1) | The lender, for the bond; no oracle |

**Tier A** is the design. Any unrestricted asset can be the collateral and any
can be the debt -- the testnet quotes markets against USDX because a borrower
wants a number they recognise, not because the covenant knows anything about it.
The fee for any of it is payable in whatever the payer holds, because no asset
is privileged here any more than anywhere else on Sequentia.

**Tier C** deserves a blunt statement. A restricted asset can only live in the
shapes its issuer permits -- for OpenAMP, a 2-of-2 enclave output with the
policy server -- and a covenant vault is not one of them. Worse, if it were, the
covenant would hand the collateral to whoever satisfied the loan's terms, which
is exactly the transfer restriction the issuer exists to enforce. Self-enforcing
collateral and issuer-enforced transferability are in direct tension. You can
have one.

So the collateral does not move. What `openampd` enforces is a **pledge**: a
record that part of a holder's balance stands behind a debt, and a transfer
check that refuses to let it leave. A holder may spend anything above their open
pledges and nothing below, and pledges accumulate, so a second lender is never
sold a claim on atoms a first lender already holds. The collateral stays in the
borrower's own enclave for the whole life of the loan.

The two operations that move value are deliberately not satisfied by the
issuer's own authority. A **release** needs the LENDER's signature, because the
lender is the only party who can say the debt was settled -- and it is the
lender's word alone: the repaid transaction id it carries is what the lender
signed over, and nothing checks that the repayment happened. A **seizure** needs
the lender's signature *and* either a matured loan or the BORROWER's
countersignature, so a lender cannot take collateral from a borrower still
inside their term. The issuer may force a release with a written reason -- a
lender who loses their key would otherwise lock the collateral forever, and a
forced release only ever returns it to its owner -- and there is deliberately no
forced seizure, because the direction that takes value away from its owner is
the direction that must not have a unilateral override. A seizure is delivered
by the issuer as an L_claw spend with two asset outputs: the pledged atoms to
the lender's enclave, the remainder straight back to the borrower's, so
settling one loan never disturbs the borrower's free balance or another
lender's collateral. Pignus builds none of that transaction and checks none of
it; it posts the request and reports the issuer's answer. Where the issuer's
key is external the seizure is two phases, like a clawback: the request, then
the issuer signing the sighashes it is handed and completing the spend.
`pledge-seize` says which of the two it got, reports delivery only for an
answer that carries a delivering transaction, and exits 4 while the collateral
is still the issuer's to move.

Pledging an asset issued *without* a clawback leaf is refused by the issuer's
policy server, the only party that can see the leaf -- Pignus checks nothing
about the asset itself -- because the lock would hold but nobody could ever
deliver on default: the collateral would freeze permanently rather than secure
anything.

What Tier C costs is not hidden anywhere: the lender's security is the issuer's
promise, not a script. A policy server that is dishonest or compromised can
release a pledge without repayment, and no amount of checking on the lender's
side would reveal it beforehand. A Tier C loan is labelled issuer-permissioned
wherever a user can see it, because presenting it quietly beside a Tier A loan
would be a lie. That is the `pledge-*` commands of `pignus-cli`, which print the
sentence `openamp.describe()` builds. `pledge-create`, `pledge-list`,
`pledge-release` and `pledge-seize` speak to `openampd` under the issuer
operator's bearer token, so they are the issuer's to run, never the
borrower's or the lender's; the two parties run `pledge-sign` with the key
they registered for their AID and hand the issuer the signature, and the
policy server's public `/v1/log`, which records every pledge, release and
seizure, is their read path. There is no pledge tab on the page and the book
carries no pledges, so those commands and that log are the whole of Tier C's
surface.

**Tier D** admits no seizure-backed loan, and the answer is structural rather
than a matter of effort. Three independent reasons, any one of which is
sufficient:

1. **The collateral cannot enter a vault.** The verifier covenant requires that
   every output carrying the restricted asset pays `C_U(Y)` for a witness-supplied
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
works, which is the point -- the restricted-asset tier ends up with the
instrument securities markets already use. Section 8.1 specifies it.

### 8.1 The repurchase, specified

A repurchase has two legs, created together and settled together.

**What it assumes of OpenDAMP, before any of it works.** These are the
issuer's decisions, not this platform's, and a repurchase against an asset
where any of them is false cannot be settled by the transaction below:

- **The asset is enforced at the network, not by a policy service.** OpenDAMP
  covenants confine a restricted asset in consensus, so every transfer spends
  the issuer's verifier output at input 0. That is what fixes the bond vault at
  input 1, and with it the output map the covenant reads. An OpenAMP
  (co-signature) asset is a different tier and belongs in Tier C.
- **Both parties are approved holders.** Leg one is an ordinary transfer,
  subject to the whitelist the issuer already publishes, so a lender who is not
  approved cannot receive the asset at all and the repurchase never starts.
  Settlement is the same transfer in reverse and needs the borrower still
  approved at that moment -- an issuer who removes them in between has stopped
  the settlement, and nothing in the covenant can override that.
- **A verifier leaf exists for the shape this settlement uses.** OpenDAMP
  compiles its verifier once per shape, and this settlement saturates `p4x6`
  exactly. An issuer whose taptree carries only the narrower `p3x5` and `p3x4`
  leaves cannot confirm it.
- **Any-asset fees, with the restricted asset barred from the fee output.**
  The settlement pays its fee in the debt asset, from the borrower's own input,
  because all four input slots are spoken for. OpenDAMP enforces absolutely
  that a fee output never carries the restricted asset, which is what makes that
  safe.
- **The wider Simplicity budget is active on the chain.** OpenDAMP covenants
  are unspendable under the one-to-one rule; on the live testnet the wider
  budget starts at a stated height, and every fresh chain has it from genesis.

`doc/sequentia/opendamp-design.md` in the node repository is authoritative for
all of these.

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

**ORIGINATION is not atomic, and that is the tier's one real exposure.**
Say it plainly, because nothing in the covenant fixes it. Leg one is an
ordinary OpenDAMP transfer and leg two is a separate funding of the bond vault,
so between them there is a window in which:

- The borrower has transferred the asset and the lender has funded nothing. The
  borrower holds no bond and no principal, and the only remedy is the lender's
  good behaviour. **This is the order to avoid**: fund the bond first.
- The lender has funded the bond and the borrower has transferred nothing. The
  bond sits in a vault whose FORFEIT leaf pays the BORROWER after
  `forfeit_after` -- so a borrower who never delivers the asset can simply wait
  and sweep it. The lender's remedy is to settle or to stop waiting long before
  that height, and there is no leaf that returns the bond to them.

Tier A does not have this problem: `build_origination` composes the whole loan
as one transaction, so the collateral and the principal move together or not at
all. A repurchase is not composed as one transaction, because leg one is a
Simplicity spend of the issuer's verifier that this repository does not build.
The mitigations are therefore procedural and belong in front of both
parties: fund the bond first, keep `forfeit_after` no further out than the
lender is willing to wait, and check both halves with `repo-verify` -- which is
why it reports `leg-one-only` and `bond-only` as distinct states and exits
non-zero on each.

**Settlement is one atomic transaction.** The borrower's payment of `debt` is
NOT covenant-checked -- the two output slots RETURN inspects are already spent
on the collateral and the bond -- so it must never be made outside the RETURN
transaction. It does not need to be checked, because the transaction cannot
confirm without both parties: the lender's `C_U(lender)` input is a Simplicity
spend under the lender's key, the borrower signs the input funding the debt,
and neither lets a transaction missing what they are owed go out. What signs
the two Simplicity inputs -- the verifier at input 0 and `C_U(lender)` at
input 2 -- is `opendamp transfer-cosign`, the OpenDAMP transfer tool's command
for a transaction it did not build; this repository signs neither. The full
shape:

| # | Input | | # | Output |
|---|---|---|---|---|
| 0 | the OpenDAMP verifier output | | 0 | the verifier output, returned |
| 1 | the bond vault | | 1 | `debt`, to the lender |
| 2 | `C_U(lender)`, holding q | | 2 | q to `C_U(borrower)` |
| 3 | the borrower's debt-asset UTXO | | 3 | the bond, to the lender |
| | | | 4 | the borrower's change |
| | | | 5 | the fee |

The vault sits at input 1 because RETURN inspects outputs `2k` and `2k+1` for
its own input index `k` (section 2.1): at input 1 those are outputs 2 and 3,
which is where the asset goes back to the borrower and the bond goes to the
lender. Input 0 is taken by the verifier, which OpenDAMP requires, and input 2
would push the pair to outputs 4 and 5. There is exactly one place the vault can
be, and it is not a matter of preference.

That is four inputs and six outputs, which is **exactly** OpenDAMP's
`N_max_inputs` and `N_max_outputs`. There is no spare slot in either direction.
Two consequences the platform must enforce, because nothing else will:

- The borrower's debt-asset side must be a **single** UTXO. Pignus refuses to
  compose a settlement otherwise; consolidate first, in its own transaction.
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
restricted asset, and `describe()` states it in those words wherever the terms
are shown. Where such a feed exists, a three-leaf variant adding
`build_liquidate_leaf` is possible; Pignus builds only the two-leaf vault.

**Naming.** The product is labelled **repurchase**, never "loan", wherever it
appears: the `repo-*` commands, the sentence `RepurchaseTerms.describe()`
builds, and the page's *Check a repurchase* tab. That sentence says in one line
that the borrower is selling the asset and holding a claim.

The loan book and the watcher do not carry repurchases. Each side verifies the
bond with `repo-verify` -- which demands the exact bond amount, because the
address cannot pin the money terms, and finds the bond's output by scanning
the funding transaction for the one paying the address the terms compile to,
as `repo-fund`, `repo-settle` and `repo-forfeit` do, since a wallet orders its
own outputs and a lender's change is not the bond; `--vout` names the output
only to tell a settled or forfeited bond from one never funded, which a scan
cannot, and a named output that is spent while the bond sits unspent at
another index is refused -- and leg one with `verify_leg_one`, and
re-verifies after the depth its risk appetite justifies: a bond whose funding an
anchor-driven reorg has undone (section 6.4) secures nothing, and there is no
watcher here to notice.

Because both halves matter, `repo-verify` reports a **state** and never an
"ok": `not-funded`, `leg-one-only`, `bond-only`, `funded-unburied`, `live`,
`forfeitable`, `settled`. `bond-only` is what a bond checked on its own earns,
and it is the answer to guard against -- a bond nobody has matched against the
transfer it secures is a number, not a claim. `leg-one-only` is its mirror and
worse: the lender is holding the asset with nothing posted for its return.
`--min-confirmations` is where each side's tolerance for a reorg goes; below it
the state is `funded-unburied` however correct both halves are. The command's
exit status says the same thing to a script: 0 for `live`, `forfeitable` and
`settled`, and 4 for every state that is not one to act on.

The settlement itself is composed in three steps, because it needs signatures
from both parties, two of them Simplicity witnesses this repository does not
produce, and the covenant's witness must go on last. `repo-settle
--skeleton` builds the transaction above from the terms and the four outpoints
it is given -- refusing a verifier coin carrying the repurchase's own asset, a
blinded coin, a `C_U` holding the wrong asset or the wrong amount, and a fee in
anything but the debt asset -- signs the borrower's debt coin with the wallet
that composes, and writes the transaction beside the four outputs it spends;
`opendamp transfer-cosign` signs the two OpenDAMP inputs and leaves every
other witness in place; and `repo-settle --attach` puts the RETURN witness on
the version everybody has signed, refusing one on which a signature is still
missing by naming the input. The vault always lands at input 1,
for the reason above; the composer places it there and that is not something a
caller can choose. A borrower whose debt coin needs no change leaves five
outputs rather than six. That is fewer, not narrower: a verifier leaf is chosen
for a SHAPE, and this settlement still spends four inputs, which the `p3x5` and
`p3x4` leaves do not allow. `p4x6` is the leaf either way.

**Settlement takes the lender's OpenDAMP key.** Inputs 0 and 2 are spends of
OpenDAMP covenants -- the verifier and the lender's `C_U` -- and the lender
signs both with `opendamp transfer-cosign`, against the issuer's current
policy snapshot, which their `openampd` serves at `GET /v1/snapshots?asset=<id>`:
the verifier witness proves the lender as sender and the
borrower as recipient under that policy, and the `C_U` witness is the lender's
signature. A lender who cannot produce that signature -- no OpenDAMP key, or
a policy that no longer names the borrower -- cannot return the asset, and the
bond is all a settlement that never comes leaves the holder with. A holder
should sell only under a lender who can, and `repo-propose` says so on every
document it writes.

## 9. Where the code lives

The platform lives in this repository, `pignus`, alongside the other
Sequentia sub-projects; the covenant and its consensus-level proof stay in the
node repository, the same way SeqOB's covenant ships with the node while the
daemon driving it ships separately.

What runs where:

- **the browser**, at `/lending/` -- derives every address, composes every
  Sequentia transaction, and asks the wallet extension only to sign and
  broadcast; the one transaction the wallet composes is the Bitcoin funding of
  a pre-vault, and the page finds the output paying the loan's own address the
  exact amount and refuses anything else. Every Bitcoin fact it shows comes
  through the book, so with no Bitcoin height it will not originate or abort.
  It
  carries the second implementations, because a browser cannot import the
  Python: `web/pignus.js` and `web/offer.js` for the covenant,
  `web/repurchase.js` for the repurchase vault, and `web/btc.js` and
  `web/adaptor.js` for the parent chain. Each is pinned byte for byte to golden
  vectors the proven Python emits (`pignus/vectors.json`,
  `web/btc_vectors.json`, `web/adaptor_vectors.json`). The loan pin is fatal --
  the page refuses to run without it -- while the repurchase and cross-chain
  pins only disable their own tabs, so a deployment whose vectors predate them
  still serves loans and refuses to check what it cannot verify. The
  borrower's side of a cross-chain loan is settled here too -- claiming the
  principal, repaying and refunding on Sequentia need no signature at all, and
  reclaiming or aborting on Bitcoin needs only the wallet's ordinary taproot
  signature; the lender's side is the responder's.
- **the wallet extension** -- holds every key. It signs its own Sequentia
  inputs and, for a cross-chain loan, the Bitcoin funding and the reclaim. It
  cannot sign a covenant leaf and exposes no x-only key to bake into one, which
  is why every exit in section 2 and both legs in section 7 are signature-free.
- **the CLI**, from `https://sequentiatestnet.com/download/` as a source
  tarball or from a clone of this repository -- the same operations against
  your own node, for anyone who would rather not use a website, plus the lender's side of
  a cross-chain loan, which is a process rather than a page: the lender draws
  the secrets, and drawing a secret in a browser means storing it in one.
- **the cross-chain relay**, the `/v1/btc/*` endpoints of `pignusd` -- carries
  offers, takes, releases and reports between borrower and lender. It holds no
  key and can move nothing, and section 7.4 says why nothing it carries has to
  be believed.
- **the lender's responder**, `pignus-cli btc-respond` on the lender's own
  machine, holding their key and their two nodes' RPC -- draws each loan's
  secret and signs each take, disburses the principal, starts the loan, claims
  the repayment and hands the borrower the secret that releases their
  collateral, takes back a principal nobody claimed, and re-sends any report
  the relay lost. Whoever
  publishes a cross-chain offer runs one; on the testnet that is the operator,
  so for native-BTC loans the operator IS a lender, an active counterparty with
  money at stake. It is the one component in that tier whose absence costs the
  other party.
- **the oracle and the loan book** -- services on the testnet server. Neither
  can move anything on its own: the book is discovery, and the oracle asserts
  a number -- and, for a native-BTC loan, co-signs a seizure under the
  conditions section 7.3 lists, which is the one place it does more than
  assert.
  `docs/api.md` in this repository documents everything both of them serve.
