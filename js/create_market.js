/**
 * OpenBook/Serum Market Creation for Raydium AMM
 *
 * DISCLAIMER: This script is part of an educational system for Solana devnet only.
 * Use exclusively on Solana devnet/testnet.
 * Do NOT use on mainnet without full legal and compliance review.
 *
 * Raydium AMM v4 requires an OpenBook (formerly Serum) market to be created first.
 * This market defines the base/quote token pair and the tick size.
 *
 * Steps:
 *   1. Create the market state account (writable, pre-funded)
 *   2. Create request queue, event queue, bids, asks accounts
 *   3. Call initializeMarket instruction on the OpenBook DEX program
 *
 * On devnet, the OpenBook program ID is different from mainnet.
 *
 * Resources:
 *   - OpenBook GitHub: https://github.com/openbook-dex/openbook-v2
 *   - Raydium docs: https://docs.raydium.io
 */

import {
  Connection,
  Keypair,
  PublicKey,
  Transaction,
  SystemProgram,
  LAMPORTS_PER_SOL,
  sendAndConfirmTransaction,
} from "@solana/web3.js";
import {
  MarketV2,
  DEVNET_PROGRAM_ID,
} from "@raydium-io/raydium-sdk-v2";
import fs from "fs";
import path from "path";
import dotenv from "dotenv";
import { fileURLToPath } from "url";

dotenv.config({ path: "../.env" });

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const NETWORK = process.env.NETWORK || "devnet";

if (NETWORK === "mainnet-beta") {
  console.error("ERROR: mainnet-beta is blocked in this educational system.");
  process.exit(1);
}

const RPC_URL = process.env.PRIMARY_RPC || "https://api.devnet.solana.com";

// OpenBook program IDs
const OPENBOOK_PROGRAM_IDS = {
  devnet: new PublicKey("EoTcMgcDRTJVZDMZWBoU6rhYHZfkNTVAPHTKrg56tYvR"),
  "testnet": new PublicKey("EoTcMgcDRTJVZDMZWBoU6rhYHZfkNTVAPHTKrg56tYvR"),
};

// ---------------------------------------------------------------------------
// Load wallet
// ---------------------------------------------------------------------------

function loadWallet() {
  const keypairPath =
    process.env.WALLET_KEYPAIR_PATH ||
    path.join(process.env.HOME, ".config/solana/devnet-test.json");
  const expanded = keypairPath.replace("~", process.env.HOME);
  const keyData = JSON.parse(fs.readFileSync(expanded, "utf-8"));
  return Keypair.fromSecretKey(Uint8Array.from(keyData));
}

// ---------------------------------------------------------------------------
// Main market creation function
// ---------------------------------------------------------------------------

/**
 * Create an OpenBook market for a base/quote token pair.
 *
 * @param {object} params
 * @param {PublicKey} params.baseMint - The base token mint (the meme coin)
 * @param {PublicKey} params.quoteMint - The quote token mint (usually SOL or USDC)
 * @param {number} params.lotSize - Base lot size (usually 1 for meme coins)
 * @param {number} params.tickSize - Minimum price tick (e.g., 0.000001)
 * @returns {Promise<{marketId: string, txids: string[]}>}
 */
export async function createOpenBookMarket({
  baseMint,
  quoteMint = new PublicKey("So11111111111111111111111111111111111111112"), // Wrapped SOL
  lotSize = 1,
  tickSize = 0.000001,
}) {
  console.log("\n=== [DEVNET] Creating OpenBook Market ===");
  console.log(`Base mint: ${baseMint.toString()}`);
  console.log(`Quote mint: ${quoteMint.toString()}`);
  console.log(`Lot size: ${lotSize} | Tick size: ${tickSize}`);

  const connection = new Connection(RPC_URL, "confirmed");
  const wallet = loadWallet();
  const openBookProgram = OPENBOOK_PROGRAM_IDS[NETWORK];

  // Check wallet balance
  const balance = await connection.getBalance(wallet.publicKey);
  console.log(`Wallet balance: ${(balance / LAMPORTS_PER_SOL).toFixed(4)} SOL`);

  if (balance < 0.5 * LAMPORTS_PER_SOL) {
    throw new Error(
      `Insufficient balance (${balance / LAMPORTS_PER_SOL} SOL). ` +
        "Need at least 0.5 SOL devnet for market creation. " +
        "Run: solana airdrop 2 --url devnet"
    );
  }

  try {
    // Use Raydium SDK's MarketV2 helper to create the market
    // This handles all the account creation and initialization internally
    const { transactions, market } = await MarketV2.makeCreateMarketInstructionSimple({
      connection,
      wallet: wallet.publicKey,
      baseInfo: {
        mint: baseMint,
        decimals: 6, // standard for Solana meme coins
      },
      quoteInfo: {
        mint: quoteMint,
        decimals: 9, // WSOL has 9 decimals
      },
      lotSize,
      tickSize,
      dexProgramId: openBookProgram,
      makeTxVersion: 0, // Legacy transaction
    });

    console.log(`\nNew market ID: ${market.toString()}`);

    // Send all setup transactions
    const txids = [];
    for (const tx of transactions) {
      const txid = await sendAndConfirmTransaction(
        connection,
        tx.transaction,
        [wallet, ...tx.signers],
        { commitment: "confirmed", maxRetries: 5 }
      );
      txids.push(txid);
      console.log(`Market creation tx: ${txid}`);
    }

    console.log(`\n✓ OpenBook market created successfully!`);
    console.log(`  Market ID: ${market.toString()}`);
    console.log(`  Transactions: ${txids.length}`);

    return {
      marketId: market.toString(),
      txids,
    };
  } catch (err) {
    console.error(`Market creation failed: ${err.message}`);
    throw err;
  }
}

// ---------------------------------------------------------------------------
// CLI entry point
// ---------------------------------------------------------------------------

// When called directly: node create_market.js <baseMint> [quoteMint]
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const baseMintArg = process.argv[2];
  const quoteMintArg = process.argv[3];

  if (!baseMintArg) {
    console.error("Usage: node create_market.js <baseMint> [quoteMint]");
    console.error("Example: node create_market.js ABC123...xyz");
    process.exit(1);
  }

  createOpenBookMarket({
    baseMint: new PublicKey(baseMintArg),
    quoteMint: quoteMintArg
      ? new PublicKey(quoteMintArg)
      : new PublicKey("So11111111111111111111111111111111111111112"),
  })
    .then((result) => {
      // Output JSON for the Python caller to parse
      console.log("\n__RESULT__");
      console.log(JSON.stringify(result));
    })
    .catch((err) => {
      console.error("FAILED:", err.message);
      process.exit(1);
    });
}
