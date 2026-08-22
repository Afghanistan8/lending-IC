# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""
LendingVault - a minimal per-user token escrow, deployed via CREATE2 by
LendingProtocol, one instance per user.

Why a vault per user, instead of one shared deposit address:
  When every user's tokens land at the SAME contract address, that contract
  can only observe "the balance grew by X" - it has no signal for WHICH
  user's transfer produced the increase, because a plain token transfer()
  carries no sender metadata to the receiving contract beyond the balance
  delta itself. Any accounting built on top of a shared address inherits
  this ambiguity, regardless of how it's structured: a caller who
  transferred nothing can still end up credited for someone else's
  transfer, as long as they call the crediting function before the
  transfer's actual sender does.

  The fix is architectural rather than a heuristic layered on top of a
  shared balance: give each user their OWN vault, deployed at a
  deterministic (CREATE2) address that only that user is ever expected to
  transfer into. A transfer to Alice's vault is Alice's, by construction -
  the lending contract never has to infer or race to figure out whose
  deposit it's looking at.

Custody model:
  - collateral, or debt-asset tokens meant to repay debt or fund
    liquidity, are transferred by the user into THEIR vault - never into
    the lending contract's own address
  - the lending contract reads a vault's balance to know what belongs to
    that vault's owner
  - only the lending contract that deployed a vault may instruct it to
    move tokens out (to the owner's wallet on withdrawal, to the pool on
    liquidation, to lending itself on repay/fund)
  - the vault carries no per-caller state, no queues, no reconciliation
    logic of its own - it holds tokens for its owner and does exactly what
    the lending contract tells it to do, nothing else

Deliberately small so it stays cheap to deploy per user and trivial to
audit.
"""

from genlayer import *


class LendingVault(gl.Contract):
    owner: Address     # the user this vault holds tokens for
    lending: Address   # the LendingProtocol that deployed this vault

    def __init__(self, owner: Address, lending: Address):
        self.owner = owner
        self.lending = lending

    @gl.public.view
    def get_owner(self) -> Address:
        return self.owner

    @gl.public.view
    def get_lending(self) -> Address:
        return self.lending

    @gl.public.write
    def forward(self, token: Address, to: Address, amount: u256) -> None:
        """Send `amount` of `token` from this vault to `to`. Only the
        deploying lending contract may call this - the vault will not move
        its owner's tokens for anyone else. The lending contract's own
        logic decides the destination (the owner's wallet on withdraw, the
        pool on liquidation, itself on repay/fund); the vault does not
        police the destination beyond trusting the lending contract that
        deployed it."""
        assert gl.message.sender_address == self.lending, "only lending"
        gl.get_contract_at(token).emit().transfer(to, amount)
