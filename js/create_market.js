/**
 * OpenBook/Serum Market Creation for Raydium AMM
 *
 * DISCLAIMER: This script is part of an educational system.
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
 * On mainnet this costs ~0.5–2 SOL in rent. On devnet it is free (use airdrop).
 *
 * Resources:
 *   - OpenBook GitHub: https://github.com/openbook-dex/openbook-v2
 *   - Raydium docs: https://docs.raydium.io
 */

import {
  Connection,
  Keypair,
  PublicKey,
  LAMPORTS_PER_SOL,
  sendAndConfirmTransaction,
  clusterApiUrl,
} from "@solana/web3.js";
import { MarketV2 } from "@raydium-io/raydium-sdk-v2";
import fs from "fs";
import path from "path";
import dotenv from "dotenv";
import { fileURLToPath } from "url";

dotenv.config({ path: "../.env" });

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ---------------------------------------------------------------------------
// Network + RPC
// ---------------------------------------------------------------------------

const NETWORK = process.env.NETWORK || "devnet";

const RPC_URL =
  process.env.PRIMARY_RPC ||
  (NETWORK === "mainnet"
    ? "https://api.mainnet-beta.solana.com"
    : clusterApiUrl("devnet"));

// ---------------------------------------------------------------------------
// Network-specific program IDs
// ---------------------------------------------------------------------------
const PROGRAM_IDS = {
  devnet: {
    openBook: new PublicKey("EoTcMgcDRTJVZDMZWBoU6rhYHZfkNTVAPHTKrg56tYvR"),
  },
  mainnet: {
    openBook: new PublicKey("srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX"),
  },
  testnet: {
    openBook: new PublicKey("EoTcMgcDRTJVZDMZWBoU6rhYHZfkNTVAPHTKrg56tYvR"),
  },
};

const PROGRAM_ID = PROGRAM_IDS[NETWORK] || PROGRAM_IDS.devnet;

// ---------------------------------------------------------------------------
// Load wallet
// ---------------------------------------------------------------------------

function loadWallet() {
  const keypairPath = (
    process.env.WALLET_KEYPAIR_PATH ||
    path.join(process.env.HOME || "~", ".config/solana/devnet-test.json")
  ).replace("~", process.env.HOME || "");
  const keyData = JSON.parse(fs.readFileSync(keypairPath, "utf-8"));
  return Keypair.fromSecretKey(Uint8Array.from(keyData));
}

// ---------------------------------------------------------------------------
// Main market creation function
// ---------------------------------------------------------------------------

/**
 * Create an OpenBook market for a base/quote token pair.
 *
 * @param {object} params
 * @param {PublicKey} params.baseMint   - The base token mint (the meme coin)
 * @param {PublicKey} params.quoteMint  - The quote token mint (usually WSOL)
 * @param {number}   params.lotSize    - Base lot size (usually 1 for meme coins)
 * @param {number}   params.tickSize   - Minimum price tick (e.g., 0.000001)
 * @returns {Promise<{marketId: string, txids: string[]}>}
 */
export async function createOpenBookMarket({
  baseMint,
  quoteMint = new PublicKey("So11111111111111111111111111111111111111112"),
  lotSize  = 1,
  tickSize = 0.000001,
}) {
  console.log(`\n=== [${NETWORK.toUpperCase()}] Creating OpenBook Market ===`);
  console.log(`Base mint:      ${baseMint.toString()}`);
  console.log(`Quote mint:     ${quoteMint.toString()}`);
  console.log(`OpenBook prog:  ${PROGRAM_ID.openBook.toString()}`);
  console.log(`Lot size: ${lotSize} | Tick size: ${tickSize}`);

  const connection = new Connection(RPC_URL, "confirmed");
  const wallet = loadWallet();

  const balance = await connection.getBalance(wallet.publicKey);
  console.log(`Wallet balance: ${(balance / LAMPORTS_PER_SOL).toFixed(4)} SOL`);

  if (balance < 0.5 * LAMPORTS_PER_SOL) {
    throw new Error(
      `Insufficient balance (${balance / LAMPORTS_PER_SOL} SOL). ` +
        `Need at least 0.5 SOL for market creation on ${NETWORK}.`
    );
  }

  try {
    const { transactions, market } = await MarketV2.makeCreateMarketInstructionSimple({
      connection,
      wallet: wallet.publicKey,
      baseInfo: {
        mint:     baseMint,
        decimals: 6,
      },
      quoteInfo: {
        mint:     quoteMint,
        decimals: 9,
      },
      lotSize,
      tickSize,
      dexProgramId:   PROGRAM_ID.openBook,
      makeTxVersion:  0,
    });

    console.log(`\nNew market ID: ${market.toString()}`);

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
    console.log(`  Market ID:    ${market.toString()}`);
    console.log(`  Transactions: ${txids.length}`);

    return { marketId: market.toString(), txids };
  } catch (err) {
    console.error(`Market creation failed: ${err.message}`);
    throw err;
  }
}

// ---------------------------------------------------------------------------
// CLI entry point
// ---------------------------------------------------------------------------

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const baseMintArg  = process.argv[2];
  const quoteMintArg = process.argv[3];

  if (!baseMintArg) {
    console.error("Usage: node create_market.js <baseMint> [quoteMint]");
    console.error("Example: node create_market.js ABC123...xyz");
    process.exit(1);
  }

  createOpenBookMarket({
    baseMint:  new PublicKey(baseMintArg),
    quoteMint: quoteMintArg
      ? new PublicKey(quoteMintArg)
      : new PublicKey("So11111111111111111111111111111111111111112"),
  })
    .then((result) => {
      console.log("\n__RESULT__");
      console.log(JSON.stringify(result));
    })
    .catch((err) => {
      console.error("FAILED:", err.message);
      process.exit(1);
    });
}
