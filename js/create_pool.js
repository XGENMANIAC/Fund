/**
 * Raydium AMM v4 Pool Creation
 *
 * DISCLAIMER: This script is part of an educational system.
 * Do NOT use on mainnet without full legal and compliance review.
 *
 * Prerequisites:
 *   1. An OpenBook market for the base/quote pair (see create_market.js)
 *   2. The deployer wallet holds both base tokens and WSOL
 *   3. Sufficient SOL for transaction fees and pool rent (~0.3 SOL on devnet,
 *      ~2–3 SOL on mainnet)
 */

import {
  Connection,
  Keypair,
  PublicKey,
  LAMPORTS_PER_SOL,
  sendAndConfirmTransaction,
  clusterApiUrl,
} from "@solana/web3.js";
import {
  MARKET_STATE_LAYOUT_V3,
  Liquidity,
  TokenAmount,
  Token,
} from "@raydium-io/raydium-sdk-v2";
import {
  TOKEN_PROGRAM_ID,
  getAssociatedTokenAddress,
} from "@solana/spl-token";
import BN from "bn.js";
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
// All addresses are canonical on-chain values — do not change without
// verifying against the Raydium and OpenBook documentation.
// ---------------------------------------------------------------------------
const PROGRAM_IDS = {
  devnet: {
    ammV4:          new PublicKey("HWy1jotHpo6UqeQxx49dpYYdQB8wj9Qk9MdxwjLvDHB8"),
    feeDestination: new PublicKey("3XMrhbv989VxAMi3DErLV9eJht1pHppW5LbKxe9fkEFR"),
    openBook:       new PublicKey("EoTcMgcDRTJVZDMZWBoU6rhYHZfkNTVAPHTKrg56tYvR"),
  },
  mainnet: {
    ammV4:          new PublicKey("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"),
    feeDestination: new PublicKey("7YttLkHDoNj9wyDur5pM1ejNaAvT9X4eqaYcHQqtj2G5"),
    openBook:       new PublicKey("srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX"),
  },
};

const PROGRAM_ID = PROGRAM_IDS[NETWORK] || PROGRAM_IDS.devnet;

// Wrapped SOL mint (same on all networks)
const WSOL_MINT = new PublicKey("So11111111111111111111111111111111111111112");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function loadWallet() {
  const keypairPath = (
    process.env.WALLET_KEYPAIR_PATH ||
    path.join(process.env.HOME || "~", ".config/solana/devnet-test.json")
  ).replace("~", process.env.HOME || "");

  const keyData = JSON.parse(fs.readFileSync(keypairPath, "utf-8"));
  return Keypair.fromSecretKey(Uint8Array.from(keyData));
}

async function getTokenBalance(connection, owner, mint) {
  const ata = await getAssociatedTokenAddress(mint, owner);
  try {
    const info = await connection.getTokenAccountBalance(ata);
    return BigInt(info.value.amount);
  } catch {
    return 0n;
  }
}

// ---------------------------------------------------------------------------
// Main pool creation function
// ---------------------------------------------------------------------------

/**
 * Create a Raydium AMM v4 pool and add initial liquidity.
 *
 * @param {object} params
 * @param {string} params.marketId       - OpenBook market ID (from create_market.js)
 * @param {string} params.baseMint       - Base token (meme coin) mint address
 * @param {number} params.baseDecimals   - Base token decimals (usually 6)
 * @param {number} params.initialSol     - Initial SOL to add as quote liquidity
 * @param {number} params.initialTokens  - Initial token amount to add as base liquidity
 * @returns {Promise<{poolId: string, lpMint: string, txids: string[]}>}
 */
export async function createRaydiumPool({
  marketId,
  baseMint: baseMintStr,
  baseDecimals = 6,
  initialSol = 0.1,
  initialTokens,
}) {
  console.log(`\n=== [${NETWORK.toUpperCase()}] Creating Raydium AMM Pool ===`);
  console.log(`Market ID:     ${marketId}`);
  console.log(`Base mint:     ${baseMintStr}`);
  console.log(`AMM program:   ${PROGRAM_ID.ammV4.toString()}`);
  console.log(`OpenBook prog: ${PROGRAM_ID.openBook.toString()}`);
  console.log(`Fee dest:      ${PROGRAM_ID.feeDestination.toString()}`);
  console.log(`Initial SOL:   ${initialSol}`);
  console.log(`Initial tokens: ${initialTokens?.toLocaleString() ?? "auto"}`);

  const connection = new Connection(RPC_URL, "confirmed");
  const wallet = loadWallet();
  const baseMint = new PublicKey(baseMintStr);

  // Check SOL balance
  const solBalance = await connection.getBalance(wallet.publicKey);
  console.log(`\nWallet SOL: ${(solBalance / LAMPORTS_PER_SOL).toFixed(4)}`);

  if (solBalance < (initialSol + 0.3) * LAMPORTS_PER_SOL) {
    throw new Error(
      `Insufficient SOL. Need ${initialSol + 0.3} SOL, have ${
        solBalance / LAMPORTS_PER_SOL
      }`
    );
  }

  // Check token balance
  const tokenBalance = await getTokenBalance(connection, wallet.publicKey, baseMint);
  const tokenBalanceReadable = Number(tokenBalance) / 10 ** baseDecimals;
  console.log(`Wallet token balance: ${tokenBalanceReadable.toLocaleString()}`);

  const tokensToAdd = initialTokens ?? Math.floor(tokenBalanceReadable * 0.8);
  if (tokenBalanceReadable < tokensToAdd) {
    throw new Error(
      `Insufficient token balance. Need ${tokensToAdd}, have ${tokenBalanceReadable}`
    );
  }

  // Fetch market info (to decode the market state)
  console.log("\nFetching market info...");
  const marketAccountInfo = await connection.getAccountInfo(new PublicKey(marketId));
  if (!marketAccountInfo) {
    throw new Error(`Market account not found: ${marketId}`);
  }

  MARKET_STATE_LAYOUT_V3.decode(marketAccountInfo.data.slice(5));

  // Build SDK token objects
  const baseToken = new Token(TOKEN_PROGRAM_ID, baseMint, baseDecimals, "TOKEN", "Token");
  const quoteToken = new Token(TOKEN_PROGRAM_ID, WSOL_MINT, 9, "SOL", "Wrapped SOL");

  const baseAmount = new TokenAmount(
    baseToken,
    new BN(tokensToAdd).mul(new BN(10 ** baseDecimals))
  );
  const quoteAmount = new TokenAmount(
    quoteToken,
    new BN(Math.floor(initialSol * LAMPORTS_PER_SOL))
  );

  console.log(`\nAdding liquidity:`);
  console.log(`  Base:  ${tokensToAdd.toLocaleString()} tokens`);
  console.log(`  Quote: ${initialSol} SOL`);
  console.log(
    `  Implied price: ${(initialSol / tokensToAdd).toFixed(12)} SOL/token`
  );

  try {
    const { transactions, poolId, lpMint } =
      await Liquidity.makeCreatePoolV4TxVersionSimple({
        connection,
        programId:  PROGRAM_ID.ammV4,
        marketInfo: {
          marketId:  new PublicKey(marketId),
          programId: PROGRAM_ID.openBook,   // OpenBook DEX program (not the market address)
        },
        baseMintInfo: {
          mint:     baseMint,
          decimals: baseDecimals,
        },
        quoteMintInfo: {
          mint:     WSOL_MINT,
          decimals: 9,
        },
        baseAmount:  baseAmount.raw,
        quoteAmount: quoteAmount.raw,
        startTime:   new BN(0),
        ownerInfo: {
          feePayer:      wallet.publicKey,
          wallet:        wallet.publicKey,
          tokenAccounts: [],
          useSOLBalance: true,
        },
        makeTxVersion:   0,
        feeDestinationId: PROGRAM_ID.feeDestination,  // Network-correct fee destination
      });

    console.log(`\nPool ID: ${poolId.toString()}`);
    console.log(`LP Mint: ${lpMint.toString()}`);

    const txids = [];
    for (const txData of transactions) {
      try {
        const txid = await sendAndConfirmTransaction(
          connection,
          txData.transaction,
          [wallet, ...txData.signers],
          { commitment: "confirmed", maxRetries: 5 }
        );
        txids.push(txid);
        console.log(`Pool init tx: ${txid}`);
      } catch (err) {
        console.error(`Transaction failed: ${err.message}`);
        throw err;
      }
    }

    console.log(`\n✓ Raydium pool created successfully!`);
    console.log(`  Pool ID: ${poolId.toString()}`);
    console.log(`  LP Mint: ${lpMint.toString()}`);
    console.log(`  Transactions: ${txids.join(", ")}`);

    return { poolId: poolId.toString(), lpMint: lpMint.toString(), txids };
  } catch (err) {
    console.error(`\nPool creation failed: ${err.message}`);
    console.error("Tip: Wait 30s after market creation before creating the pool.");
    throw err;
  }
}

// ---------------------------------------------------------------------------
// CLI entry point
// ---------------------------------------------------------------------------

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  if (args.length < 2) {
    console.error(
      "Usage: node create_pool.js <marketId> <baseMint> [initialSol] [initialTokens]"
    );
    process.exit(1);
  }

  const [marketId, baseMint, initialSolArg, initialTokensArg] = args;

  createRaydiumPool({
    marketId,
    baseMint,
    initialSol:    initialSolArg    ? parseFloat(initialSolArg)  : 0.1,
    initialTokens: initialTokensArg ? parseInt(initialTokensArg) : undefined,
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
