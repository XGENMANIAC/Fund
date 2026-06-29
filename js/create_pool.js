/**
 * Raydium AMM v4 Pool Creation
 *
 * DISCLAIMER: Do NOT use on mainnet without full legal and compliance review.
 *
 * Prerequisites:
 *   1. An OpenBook market (see create_market.js)
 *   2. Wallet holds base tokens + enough SOL (~0.3 SOL devnet, ~2-3 SOL mainnet)
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

const WSOL_MINT = new PublicKey("So11111111111111111111111111111111111111112");

// ---------------------------------------------------------------------------
// Wallet loader — three sources in priority order:
//   1. WALLET_PRIVATE_KEY  — base58 string exported from Solflare / Phantom
//   2. WALLET_KEYPAIR_JSON — base64-encoded JSON byte array (Railway / cloud)
//   3. WALLET_KEYPAIR_PATH — local .json keypair file
// ---------------------------------------------------------------------------
import bs58 from "bs58";

function loadWallet() {
  // 1. Solflare / Phantom base58 private key
  const b58Key = process.env.WALLET_PRIVATE_KEY;
  if (b58Key) {
    const raw = bs58.decode(b58Key.trim());
    if (raw.length !== 64 && raw.length !== 32) {
      throw new Error(
        `WALLET_PRIVATE_KEY decoded to ${raw.length} bytes — expected 64 (full keypair) or 32 (seed). ` +
        "Copy the full Private Key from Solflare → Settings → Security → Export Private Key."
      );
    }
    return Keypair.fromSecretKey(raw.length === 64 ? raw : Keypair.fromSeed(raw).secretKey);
  }

  // 2. Base64-encoded JSON array (cloud/Railway)
  const keypairJsonEnv = process.env.WALLET_KEYPAIR_JSON;
  if (keypairJsonEnv) {
    try {
      const decoded = Buffer.from(keypairJsonEnv, "base64").toString("utf-8");
      return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(decoded)));
    } catch {
      return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(keypairJsonEnv)));
    }
  }

  // 3. Local keypair file
  const keypairPath = (
    process.env.WALLET_KEYPAIR_PATH ||
    path.join(process.env.HOME || "~", ".config/solana/mainnet.json")
  ).replace("~", process.env.HOME || "");
  return Keypair.fromSecretKey(
    Uint8Array.from(JSON.parse(fs.readFileSync(keypairPath, "utf-8")))
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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
// Main pool creation
// ---------------------------------------------------------------------------

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

  const connection = new Connection(RPC_URL, "confirmed");
  const wallet = loadWallet();
  const baseMint = new PublicKey(baseMintStr);

  const solBalance = await connection.getBalance(wallet.publicKey);
  console.log(`\nWallet SOL: ${(solBalance / LAMPORTS_PER_SOL).toFixed(4)}`);

  if (solBalance < (initialSol + 0.3) * LAMPORTS_PER_SOL) {
    throw new Error(
      `Insufficient SOL. Need ${initialSol + 0.3}, have ${solBalance / LAMPORTS_PER_SOL}`
    );
  }

  const tokenBalance = await getTokenBalance(connection, wallet.publicKey, baseMint);
  const tokenBalanceReadable = Number(tokenBalance) / 10 ** baseDecimals;
  const tokensToAdd = initialTokens ?? Math.floor(tokenBalanceReadable * 0.8);

  if (tokenBalanceReadable < tokensToAdd) {
    throw new Error(`Insufficient tokens. Need ${tokensToAdd}, have ${tokenBalanceReadable}`);
  }

  console.log(`\nFetching market info...`);
  const marketAccountInfo = await connection.getAccountInfo(new PublicKey(marketId));
  if (!marketAccountInfo) throw new Error(`Market account not found: ${marketId}`);

  MARKET_STATE_LAYOUT_V3.decode(marketAccountInfo.data.slice(5));

  const baseToken  = new Token(TOKEN_PROGRAM_ID, baseMint, baseDecimals, "TOKEN", "Token");
  const quoteToken = new Token(TOKEN_PROGRAM_ID, WSOL_MINT, 9, "SOL", "Wrapped SOL");

  const baseAmount  = new TokenAmount(baseToken,  new BN(tokensToAdd).mul(new BN(10 ** baseDecimals)));
  const quoteAmount = new TokenAmount(quoteToken, new BN(Math.floor(initialSol * LAMPORTS_PER_SOL)));

  console.log(`\nAdding liquidity:`);
  console.log(`  Base:  ${tokensToAdd.toLocaleString()} tokens`);
  console.log(`  Quote: ${initialSol} SOL`);

  try {
    const { transactions, poolId, lpMint } =
      await Liquidity.makeCreatePoolV4TxVersionSimple({
        connection,
        programId:         PROGRAM_ID.ammV4,
        marketInfo: {
          marketId:  new PublicKey(marketId),
          programId: PROGRAM_ID.openBook,
        },
        baseMintInfo:  { mint: baseMint,  decimals: baseDecimals },
        quoteMintInfo: { mint: WSOL_MINT, decimals: 9 },
        baseAmount:   baseAmount.raw,
        quoteAmount:  quoteAmount.raw,
        startTime:    new BN(0),
        ownerInfo: {
          feePayer:      wallet.publicKey,
          wallet:        wallet.publicKey,
          tokenAccounts: [],
          useSOLBalance: true,
        },
        makeTxVersion:    0,
        feeDestinationId: PROGRAM_ID.feeDestination,
      });

    const txids = [];
    for (const txData of transactions) {
      const txid = await sendAndConfirmTransaction(
        connection, txData.transaction,
        [wallet, ...txData.signers],
        { commitment: "confirmed", maxRetries: 5 }
      );
      txids.push(txid);
      console.log(`Pool tx: ${txid}`);
    }

    console.log(`\n✓ Pool created: ${poolId.toString()}`);
    return { poolId: poolId.toString(), lpMint: lpMint.toString(), txids };

  } catch (err) {
    console.error(`Pool creation failed: ${err.message}`);
    console.error("Tip: wait 30s after market creation before creating the pool.");
    throw err;
  }
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [,, marketId, baseMint, initialSolArg, initialTokensArg] = process.argv;
  if (!marketId || !baseMint) {
    console.error("Usage: node create_pool.js <marketId> <baseMint> [initialSol] [initialTokens]");
    process.exit(1);
  }
  createRaydiumPool({
    marketId,
    baseMint,
    initialSol:    initialSolArg    ? parseFloat(initialSolArg)  : 0.1,
    initialTokens: initialTokensArg ? parseInt(initialTokensArg) : undefined,
  })
    .then(r  => { console.log("\n__RESULT__"); console.log(JSON.stringify(r)); })
    .catch(e => { console.error("FAILED:", e.message); process.exit(1); });
}
