/**
 * High-level Raydium deployment orchestrator.
 *
 * DISCLAIMER: This script is part of an educational system for Solana devnet only.
 * Use exclusively on Solana devnet/testnet.
 * Do NOT use on mainnet without full legal and compliance review.
 *
 * This is the main entry point called by the Python deploy/raydium.py module
 * via subprocess. It reads deployment parameters from a JSON file and
 * orchestrates the full deploy sequence:
 *
 *   1. Create OpenBook market
 *   2. Wait for market confirmation (important — pool creation fails otherwise)
 *   3. Create Raydium AMM pool with initial liquidity
 *   4. Output the result as JSON for Python to parse
 *
 * Input: JSON file path as first argument
 * Output: JSON to stdout with market ID, pool ID, LP mint, all tx signatures
 */

import { createOpenBookMarket } from "./create_market.js";
import { createRaydiumPool } from "./create_pool.js";
import { PublicKey } from "@solana/web3.js";
import fs from "fs";
import { fileURLToPath } from "url";

// ---------------------------------------------------------------------------
// Safety check
// ---------------------------------------------------------------------------

const NETWORK = process.env.NETWORK || "devnet";
if (NETWORK === "mainnet-beta") {
  console.error(JSON.stringify({
    success: false,
    error: "MAINNET BLOCKED: This educational system refuses to operate on mainnet-beta.",
  }));
  process.exit(1);
}

// ---------------------------------------------------------------------------
// Main orchestrator
// ---------------------------------------------------------------------------

async function deployToRaydium(params) {
  const {
    mint_address: mintAddress,
    token_name: tokenName,
    token_symbol: tokenSymbol,
    decimals = 6,
    initial_sol: initialSol = 0.1,
    initial_tokens: initialTokens,
    quote_mint: quoteMint = "So11111111111111111111111111111111111111112",
    lot_size: lotSize = 1,
    tick_size: tickSize = 0.000001,
  } = params;

  console.log(`\n${"=".repeat(60)}`);
  console.log(`[EDUCATIONAL DEVNET DEPLOY]`);
  console.log(`Token: ${tokenName} (${tokenSymbol})`);
  console.log(`Mint: ${mintAddress}`);
  console.log(`Network: ${NETWORK}`);
  console.log(`${"=".repeat(60)}\n`);

  const result = {
    success: false,
    network: NETWORK,
    mint_address: mintAddress,
    token_name: tokenName,
    token_symbol: tokenSymbol,
    market_id: null,
    pool_id: null,
    lp_mint: null,
    market_txids: [],
    pool_txids: [],
    error: null,
  };

  try {
    // Step 1: Create OpenBook market
    console.log("Step 1/3: Creating OpenBook market...");
    const marketResult = await createOpenBookMarket({
      baseMint: new PublicKey(mintAddress),
      quoteMint: new PublicKey(quoteMint),
      lotSize,
      tickSize,
    });

    result.market_id = marketResult.marketId;
    result.market_txids = marketResult.txids;

    // Step 2: Wait for market to fully confirm before pool creation
    // This is critical — Raydium will fail if market isn't fully propagated
    const waitSeconds = 15;
    console.log(`\nStep 2/3: Waiting ${waitSeconds}s for market confirmation...`);
    await new Promise((resolve) => setTimeout(resolve, waitSeconds * 1000));

    // Step 3: Create Raydium AMM pool
    console.log("Step 3/3: Creating Raydium AMM pool...");
    const poolResult = await createRaydiumPool({
      marketId: marketResult.marketId,
      baseMint: mintAddress,
      baseDecimals: decimals,
      initialSol,
      initialTokens,
    });

    result.pool_id = poolResult.poolId;
    result.lp_mint = poolResult.lpMint;
    result.pool_txids = poolResult.txids;
    result.success = true;

    console.log(`\n${"=".repeat(60)}`);
    console.log(`[SUCCESS] Deployment complete!`);
    console.log(`  Market ID: ${result.market_id}`);
    console.log(`  Pool ID:   ${result.pool_id}`);
    console.log(`  LP Mint:   ${result.lp_mint}`);
    console.log(`${"=".repeat(60)}`);

  } catch (err) {
    result.error = err.message;
    console.error(`\n[FAILED] Deployment error: ${err.message}`);
  }

  return result;
}

// ---------------------------------------------------------------------------
// Entry point (called by Python subprocess)
// ---------------------------------------------------------------------------

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const paramsFile = process.argv[2];

  if (!paramsFile) {
    console.error(JSON.stringify({
      success: false,
      error: "No params file provided. Usage: node raydium_deploy.js <params.json>",
    }));
    process.exit(1);
  }

  let params;
  try {
    params = JSON.parse(fs.readFileSync(paramsFile, "utf-8"));
  } catch (err) {
    console.error(JSON.stringify({
      success: false,
      error: `Failed to parse params file: ${err.message}`,
    }));
    process.exit(1);
  }

  deployToRaydium(params)
    .then((result) => {
      // Output structured JSON for Python to parse
      console.log("\n__RESULT__");
      console.log(JSON.stringify(result, null, 2));
      process.exit(result.success ? 0 : 1);
    })
    .catch((err) => {
      console.error(JSON.stringify({ success: false, error: err.message }));
      process.exit(1);
    });
}

export { deployToRaydium };
