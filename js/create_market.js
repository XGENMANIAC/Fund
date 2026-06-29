/**
 * OpenBook Market Creation for Raydium AMM
 *
 * DISCLAIMER: Do NOT use on mainnet without full legal and compliance review.
 *
 * Mainnet cost: ~0.5-2 SOL in account rent.
 * Devnet cost:  free (use `solana airdrop`).
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
  devnet:  { openBook: new PublicKey("EoTcMgcDRTJVZDMZWBoU6rhYHZfkNTVAPHTKrg56tYvR") },
  testnet: { openBook: new PublicKey("EoTcMgcDRTJVZDMZWBoU6rhYHZfkNTVAPHTKrg56tYvR") },
  mainnet: { openBook: new PublicKey("srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX")  },
};

const PROGRAM_ID = PROGRAM_IDS[NETWORK] || PROGRAM_IDS.devnet;

// ---------------------------------------------------------------------------
// Wallet loader — supports WALLET_KEYPAIR_JSON (base64 or raw JSON array)
// ---------------------------------------------------------------------------
function loadWallet() {
  const keypairJsonEnv = process.env.WALLET_KEYPAIR_JSON;
  if (keypairJsonEnv) {
    try {
      const decoded = Buffer.from(keypairJsonEnv, "base64").toString("utf-8");
      return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(decoded)));
    } catch {
      return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(keypairJsonEnv)));
    }
  }
  const keypairPath = (
    process.env.WALLET_KEYPAIR_PATH ||
    path.join(process.env.HOME || "~", ".config/solana/mainnet.json")
  ).replace("~", process.env.HOME || "");
  return Keypair.fromSecretKey(
    Uint8Array.from(JSON.parse(fs.readFileSync(keypairPath, "utf-8")))
  );
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

export async function createOpenBookMarket({
  baseMint,
  quoteMint = new PublicKey("So11111111111111111111111111111111111111112"),
  lotSize  = 1,
  tickSize = 0.000001,
}) {
  console.log(`\n=== [${NETWORK.toUpperCase()}] Creating OpenBook Market ===`);
  console.log(`Base:          ${baseMint.toString()}`);
  console.log(`Quote:         ${quoteMint.toString()}`);
  console.log(`OpenBook prog: ${PROGRAM_ID.openBook.toString()}`);

  const connection = new Connection(RPC_URL, "confirmed");
  const wallet = loadWallet();

  const balance = await connection.getBalance(wallet.publicKey);
  console.log(`Wallet SOL: ${(balance / LAMPORTS_PER_SOL).toFixed(4)}`);

  if (balance < 0.5 * LAMPORTS_PER_SOL) {
    throw new Error(
      `Insufficient balance (${balance / LAMPORTS_PER_SOL} SOL). Need at least 0.5 SOL.`
    );
  }

  const { transactions, market } = await MarketV2.makeCreateMarketInstructionSimple({
    connection,
    wallet:        wallet.publicKey,
    baseInfo:  { mint: baseMint,  decimals: 6 },
    quoteInfo: { mint: quoteMint, decimals: 9 },
    lotSize,
    tickSize,
    dexProgramId:  PROGRAM_ID.openBook,
    makeTxVersion: 0,
  });

  console.log(`\nNew market ID: ${market.toString()}`);

  const txids = [];
  for (const tx of transactions) {
    const txid = await sendAndConfirmTransaction(
      connection, tx.transaction,
      [wallet, ...tx.signers],
      { commitment: "confirmed", maxRetries: 5 }
    );
    txids.push(txid);
    console.log(`Market tx: ${txid}`);
  }

  console.log(`\n✓ Market created: ${market.toString()}`);
  return { marketId: market.toString(), txids };
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [,, baseMintArg, quoteMintArg] = process.argv;
  if (!baseMintArg) {
    console.error("Usage: node create_market.js <baseMint> [quoteMint]");
    process.exit(1);
  }
  createOpenBookMarket({
    baseMint:  new PublicKey(baseMintArg),
    quoteMint: quoteMintArg
      ? new PublicKey(quoteMintArg)
      : new PublicKey("So11111111111111111111111111111111111111112"),
  })
    .then(r  => { console.log("\n__RESULT__"); console.log(JSON.stringify(r)); })
    .catch(e => { console.error("FAILED:", e.message); process.exit(1); });
}
