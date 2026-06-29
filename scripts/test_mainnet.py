"""
Mainnet deployment test script.

Runs in two modes:

  python scripts/test_mainnet.py
           Check RPC connectivity and wallet balance only. No spend.

  python scripts/test_mainnet.py --deploy
           Full test: mint an SPL token + create a Raydium pool on mainnet.
           Costs approximately 1-3 SOL (OpenBook market rent is the bulk).
           All transaction signatures are printed for Solscan verification.

Environment:
  Set NETWORK=mainnet and WALLET_KEYPAIR_JSON (or WALLET_KEYPAIR_PATH)
  before running, or copy .env.example to .env and fill in values.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NETWORK", "mainnet")

SOLSCAN = "https://solscan.io"


# ---------------------------------------------------------------------------
# Connectivity check
# ---------------------------------------------------------------------------

async def check_connectivity() -> tuple[str, float]:
    """Verify RPC is reachable and print wallet info. Returns (address, balance)."""
    from utils.helpers import load_env
    from utils.rpc import SolanaRPCClient
    from utils.keyloader import get_keypair_path

    load_env()

    print("\n" + "=" * 56)
    print("  MAINNET CONNECTIVITY CHECK")
    print("=" * 56)

    async with SolanaRPCClient(network="mainnet-beta") as client:
        slot = await client.get_slot()
        print(f"\n  RPC OK — current slot: {slot:,}")

        keypair_path = get_keypair_path()
        with open(keypair_path) as f:
            secret = bytes(json.load(f))

        # Use solders if available, otherwise read pubkey from account info
        try:
            from solders.keypair import Keypair  # type: ignore
            kp = Keypair.from_bytes(secret)
            address = str(kp.pubkey())
        except ImportError:
            # Fallback: derive pubkey via solana-py
            from solana.keypair import Keypair  # type: ignore
            kp = Keypair.from_secret_key(secret[:32])
            address = str(kp.public_key)

        balance = await client.get_balance(address)

        print(f"  Wallet:  {address}")
        print(f"  Balance: {balance:.6f} SOL")
        print(f"  Solscan: {SOLSCAN}/account/{address}")

        if balance < 0.1:
            print("\n  ERROR: balance too low. Fund the wallet before testing.")
        elif balance < 2.0:
            print("\n  WARNING: balance < 2 SOL.")
            print("  OpenBook market creation costs 0.5-2 SOL on mainnet.")
            print("  Proceed with caution.")
        else:
            print("\n  Balance sufficient for a full test deployment.")

    return address, balance


# ---------------------------------------------------------------------------
# Full deployment test
# ---------------------------------------------------------------------------

async def run_deploy_test() -> None:
    """Mint a token and create a Raydium pool on mainnet."""
    from utils.helpers import load_env, load_config
    from storage.database import initialize_db
    from analysis.generator import GeneratedTokenSpec
    from deploy.spl_token import SPLTokenDeployer
    from deploy.raydium import RaydiumDeployer

    address, balance = await check_connectivity()

    print("\n" + "=" * 56)
    print("  FULL MAINNET DEPLOYMENT TEST")
    print("=" * 56)
    print(f"\n  Wallet:  {address}")
    print(f"  Balance: {balance:.6f} SOL")
    print("\n  This will spend real SOL:")
    print("    ~0.002 SOL  — SPL token mint")
    print("    ~0.5-2 SOL  — OpenBook market creation")
    print("    ~0.1 SOL    — initial pool liquidity")
    print("    ~0.05 SOL   — transaction fees")
    print("\n  Total estimate: 0.65 - 2.15 SOL\n")

    confirm = input("  Type 'YES_MAINNET' to proceed: ").strip()
    if confirm != "YES_MAINNET":
        print("  Aborted.")
        return

    load_env()
    initialize_db()

    # Minimal test token spec
    spec = GeneratedTokenSpec(
        source_name="Test",
        source_symbol="SRC",
        new_name="MainnetTest",
        new_symbol="MNTT",
        new_decimals=6,
        total_supply=1_000_000_000,
        pool_tokens=800_000_000,
        retained_tokens=200_000_000,
        metadata_json={
            "name": "MainnetTest",
            "symbol": "MNTT",
            "description": "Mainnet deployment verification token",
            "image": "",
        },
    )

    print("\nStep 1/2: Deploying SPL token mint...")
    token_deployer = SPLTokenDeployer()
    token_result = await token_deployer.deploy_token(spec, deployment_id=None)
    mint_address = token_result["mint_address"]
    print(f"  Mint address: {mint_address}")
    print(f"  Solscan:      {SOLSCAN}/token/{mint_address}")

    print("\nStep 2/2: Creating Raydium pool...")
    raydium = RaydiumDeployer()
    pool_result = await raydium.deploy(
        spec=spec,
        mint_address=mint_address,
        deployment_id=None,
        initial_tokens=spec.pool_tokens,
    )

    print("\n" + "=" * 56)
    if pool_result.get("success"):
        pool_id   = pool_result.get("pool_id", "")
        market_id = pool_result.get("market_id", "")
        print("  RESULT: SUCCESS")
        print(f"  Mint:      {mint_address}")
        print(f"  Market ID: {market_id}")
        print(f"  Pool ID:   {pool_id}")
        print(f"\n  Verify on Solscan:")
        print(f"    Token:  {SOLSCAN}/token/{mint_address}")
        print(f"    Pool:   {SOLSCAN}/account/{pool_id}")
        print(f"    Market: {SOLSCAN}/account/{market_id}")
    else:
        print("  RESULT: FAILED")
        print(f"  Error: {pool_result.get('error', 'unknown')}")
    print("=" * 56)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mainnet connectivity and deployment test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/test_mainnet.py             # connectivity only
  python scripts/test_mainnet.py --deploy    # full token + pool deployment
        """,
    )
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="Run full token mint + Raydium pool deployment (~1-3 SOL)",
    )
    args = parser.parse_args()

    if args.deploy:
        asyncio.run(run_deploy_test())
    else:
        asyncio.run(check_connectivity())
        print("\n  To run a full deployment test:")
        print("    python scripts/test_mainnet.py --deploy\n")


if __name__ == "__main__":
    main()
