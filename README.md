# Solana Meme Coin Detection & Educational Cloning System

> **⚠️ DISCLAIMER — READ BEFORE PROCEEDING**
>
> This project is **purely for educational, simulation, and research purposes**.
> All code, documentation, and examples are designed to explore the technical
> credibility, speed, reliability, detection accuracy, and limitations of
> automated systems in the meme coin ecosystem.
>
> **Use exclusively on Solana devnet/testnet.**
> **Do NOT deploy to mainnet without full legal, financial, and compliance review.**
>
> Creating, launching, and promoting tokens may be subject to securities laws,
> financial regulations, and platform terms of service in your jurisdiction.
> The authors assume no liability for misuse of this software.

---

## What This System Does (Educational Overview)

This system demonstrates a complete automated pipeline for:

1. **Detecting** trending meme coins across Solana via on-chain data and scrapers
2. **Analyzing** token signals (volume, holders, social buzz) with a risk score
3. **Forking/Cloning** a new SPL token with custom metadata
4. **Deploying** a Raydium liquidity pool on devnet
5. **Monitoring** the launched token's post-deployment metrics

The goal is to understand — and critically evaluate — how fast, reliable, and
accurate such systems can be, and what their real-world limitations are.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                 MONITORING LAYER                        │
│  DexScreener API │ pump.fun (Playwright) │ Solana RPC  │
│  Social Search   │ Birdeye scraper       │ Aggregator  │
└────────────────────────┬────────────────────────────────┘
                         │ Structured alerts
┌────────────────────────▼────────────────────────────────┐
│                 ANALYSIS LAYER                          │
│  Virality Scorer │ Rug-Risk Heuristics │ Token Generator│
└────────────────────────┬────────────────────────────────┘
                         │ Approved candidates
┌────────────────────────▼────────────────────────────────┐
│              MANUAL APPROVAL GATE ✋                    │
│         (required — never bypassed)                    │
└────────────────────────┬────────────────────────────────┘
                         │ Human-approved
┌────────────────────────▼────────────────────────────────┐
│                 DEPLOYMENT LAYER                        │
│  SPL Token Mint │ Metadata (IPFS/URI) │ Raydium Pool   │
│  OpenBook Market│ Authority Revocation│                 │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                POST-LAUNCH MONITORING                   │
│  Pool metrics │ Holder tracking │ Promotion simulation  │
└─────────────────────────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│               STREAMLIT DASHBOARD                       │
│       View detections, approvals, live metrics         │
└─────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
solana-meme-detector/
├── README.md
├── config.example.yaml         # Copy to config.yaml and fill in
├── .env.example                # Copy to .env and fill in
├── requirements.txt            # Python dependencies
├── package.json                # Node.js dependencies (Raydium SDK)
│
├── monitor/                    # Detection & scraping
│   ├── dexscreener.py          # DexScreener free API client
│   ├── pumpfun.py              # pump.fun Playwright scraper
│   ├── social.py               # X/Twitter public search scraper
│   ├── onchain.py              # Solana RPC monitoring
│   └── aggregator.py           # Combines all sources + deduplication
│
├── analysis/                   # Scoring & token generation
│   ├── scorer.py               # Virality + rug-risk scoring
│   ├── generator.py            # New token name/symbol/metadata gen
│   └── filters.py              # Configurable threshold filters
│
├── deploy/                     # On-chain deployment (devnet only)
│   ├── spl_token.py            # SPL token mint creation
│   ├── metadata.py             # Metadata upload (NFT.storage/pinata free)
│   ├── raydium.py              # Python wrapper calling Node.js deployer
│   └── pipeline.py             # Full deploy pipeline
│
├── js/                         # Node.js — Raydium SDK (no Python equivalent)
│   ├── package.json
│   ├── create_market.js        # OpenBook/Serum market creation
│   ├── create_pool.js          # Raydium AMM pool creation
│   └── raydium_deploy.js       # High-level deploy orchestrator
│
├── bot/                        # Main orchestration
│   ├── main.py                 # Entry point — runs the full loop
│   ├── approval.py             # Approval gate (CLI + DB)
│   └── pipeline.py             # Wires monitor → analyze → approve → deploy
│
├── dashboard/                  # Streamlit UI
│   ├── app.py                  # Main app entry
│   ├── pages/
│   │   ├── detections.py       # Live detection feed
│   │   ├── approvals.py        # Approval queue
│   │   └── monitoring.py       # Post-launch metrics
│   └── components/
│       └── charts.py           # Reusable chart helpers
│
├── storage/                    # SQLite persistence
│   ├── database.py             # DB connection + helpers
│   └── schema.sql              # Schema definition
│
├── utils/                      # Shared utilities
│   ├── rpc.py                  # RPC client with fallback + rate-limit handling
│   ├── logger.py               # Structured logging
│   └── helpers.py              # Misc helpers
│
└── tests/                      # Unit tests
    ├── test_monitor.py
    ├── test_analysis.py
    └── test_deploy.py
```

---

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 18+
- Git
- A funded **devnet** Solana wallet (use `solana airdrop`)

### 1 — Clone and install

```bash
git clone <this-repo>
cd solana-meme-detector

# Python dependencies
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Playwright browsers
playwright install chromium

# Node.js dependencies (Raydium SDK)
cd js && npm install && cd ..
```

### 2 — Configure

```bash
cp config.example.yaml config.yaml
cp .env.example .env
# Edit both files — set your devnet wallet path and thresholds
```

### 3 — Initialize the database

```bash
python -m storage.database --init
```

### 4 — Run the monitor (detection only, safe)

```bash
python -m bot.main --mode monitor-only
```

### 5 — Launch the dashboard

```bash
streamlit run dashboard/app.py
```

### 6 — Full pipeline (devnet only, with approval gate)

```bash
python -m bot.main --mode full --network devnet
```

---

## Devnet Setup

```bash
# Install Solana CLI
sh -c "$(curl -sSfL https://release.solana.com/stable/install)"

# Configure for devnet
solana config set --url https://api.devnet.solana.com

# Generate a new keypair (NEVER use mainnet wallets here)
solana-keygen new --outfile ~/.config/solana/devnet-test.json

# Airdrop 2 SOL (devnet faucet — free)
solana airdrop 2 --keypair ~/.config/solana/devnet-test.json

# Check balance
solana balance --keypair ~/.config/solana/devnet-test.json
```

---

## Configuration Reference

See `config.example.yaml` for all available options. Key settings:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `network` | `devnet` | `devnet` or `testnet` — never `mainnet-beta` |
| `monitor.poll_interval` | 60 | Seconds between scrape cycles |
| `filters.min_liquidity_usd` | 5000 | Minimum $ liquidity to consider |
| `filters.min_volume_5m` | 500 | Minimum 5-min volume |
| `filters.min_score` | 60 | Score threshold (0–100) to queue |
| `deploy.initial_sol` | 0.1 | SOL to seed pool (devnet only) |
| `deploy.token_supply` | 1_000_000_000 | Total token supply |

---

## Educational Discussion: System Credibility & Limitations

### What Works Well (Technically)

- **Detection speed**: DexScreener's free API updates ~30s behind on-chain. Playwright
  scrapers can add another 5–15s of latency. Total: ~45s from on-chain event to alert.
- **On-chain RPC monitoring**: Logs subscriptions via WebSocket give near-real-time
  new pool events but are rate-limited on free public RPCs.
- **SPL token creation**: Fully automatable in <5 seconds on devnet.
- **Raydium AMM pool creation**: ~10–20 seconds including OpenBook market setup.

### Real-World Limitations

- **Front-running**: In practice, profitable meme coin snipers operate at the
  validator level or via private RPC endpoints, achieving <1s latency. This
  educational system cannot compete with that.
- **Rug detection accuracy**: On-chain heuristics (mint authority not revoked,
  concentrated holders, no liquidity lock) catch obvious rugs but miss sophisticated ones.
- **Social signal lag**: Scraping Twitter adds 30–120s of lag vs. paid streaming APIs.
- **Rate limits**: Free public RPCs (mainnet) enforce strict rate limits that make
  continuous monitoring unreliable without custom infrastructure.
- **Copy-cat token success**: Statistically, copy-cat tokens fail >99% of the time.
  The original's community does not follow to forks.

### Ethical & Legal Considerations

1. Automated token launches can constitute market manipulation.
2. Promoting tokens you hold is a conflict of interest and potentially securities fraud.
3. Many jurisdictions treat meme coins as unregistered securities.
4. pump.fun and other platforms prohibit automated bot activity in their ToS.
5. **This system must only be used on devnet for educational purposes.**

---

## Safety Checklist

- [ ] Wallet is devnet-only with no real funds
- [ ] `NETWORK=devnet` in `.env`
- [ ] Approval gate is ENABLED in config
- [ ] You understand the legal landscape in your jurisdiction
- [ ] You have read and understood this README fully

---

## License

MIT License — for educational use only. See LICENSE file.
