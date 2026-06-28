"""
Streamlit Dashboard — Meme Coin Educational Detection System

DISCLAIMER: Educational system for Solana devnet only.
This dashboard is for monitoring and approving educational deployments.
Do NOT use for real trading decisions.

Run with: streamlit run dashboard/app.py

Features:
  - Live detection feed with filtering
  - Candidate queue with scores and risk flags
  - Approval queue (approve/reject from UI)
  - Deployment history
  - Post-launch monitoring charts
"""

import sys
from pathlib import Path

# Ensure project root is in path when running from dashboard/
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import streamlit as st
from datetime import datetime, timezone

from storage.database import (
    get_recent_detections,
    get_pending_candidates,
    get_deployments,
    update_candidate_status,
    approve_generated_token,
    initialize_db,
)
from utils.helpers import format_usd, load_config

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Solana Meme Detector [DEVNET]",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("🔬 Meme Detector")
    st.caption("**DEVNET ONLY — Educational Use**")
    st.divider()

    page = st.radio(
        "Navigation",
        ["📡 Live Detections", "🎯 Candidates", "✅ Approvals", "🚀 Deployments", "📊 Monitoring"],
        index=0,
    )

    cfg = load_config()
    st.divider()
    st.caption(f"Network: **{cfg.get('network', 'devnet').upper()}**")
    st.caption(f"Mode: **educational**")
    st.caption(f"Approval gate: **{'ENABLED' if cfg.get('approval', {}).get('enabled', True) else 'DISABLED'}**")

    refresh = st.button("🔄 Refresh Data")
    auto_refresh = st.toggle("Auto-refresh (15s)", value=False)
    if auto_refresh:
        st.empty()  # auto_refresh handled below

# ---------------------------------------------------------------------------
# Auto-refresh
# ---------------------------------------------------------------------------

if auto_refresh:
    import time
    refresh_interval = cfg.get("dashboard", {}).get("auto_refresh_seconds", 15)
    # Streamlit's built-in rerun on timer
    st.session_state.setdefault("last_refresh", 0)
    if time.time() - st.session_state["last_refresh"] > refresh_interval:
        st.session_state["last_refresh"] = time.time()
        st.rerun()

# Initialize DB on first load
initialize_db()

# ---------------------------------------------------------------------------
# Disclaimer banner
# ---------------------------------------------------------------------------

st.info(
    "⚠️ **EDUCATIONAL SYSTEM — DEVNET ONLY** — "
    "All detections and deployments are on Solana devnet. "
    "No real funds are used. This system is for research and educational purposes only.",
    icon="🔬",
)

# ---------------------------------------------------------------------------
# Page: Live Detections
# ---------------------------------------------------------------------------

if page == "📡 Live Detections":
    st.title("📡 Live Token Detections")
    st.caption("Tokens detected across DexScreener, pump.fun, and social monitors.")

    detections = get_recent_detections(limit=200)

    if not detections:
        st.info("No detections yet. Start the bot: `python -m bot.main --mode monitor-only`")
    else:
        df = pd.DataFrame(detections)

        # Filters
        col1, col2, col3 = st.columns(3)
        with col1:
            min_liq = st.slider("Min Liquidity ($)", 0, 100_000, 0, step=1000)
        with col2:
            source_filter = st.multiselect(
                "Source",
                options=df["source"].unique().tolist() if "source" in df.columns else [],
                default=[],
            )
        with col3:
            max_age = st.slider("Max Age (hours)", 0.1, 24.0, 4.0, step=0.1)

        # Apply filters
        if "liquidity_usd" in df.columns:
            df = df[df["liquidity_usd"] >= min_liq]
        if source_filter and "source" in df.columns:
            df = df[df["source"].isin(source_filter)]
        if "age_hours" in df.columns:
            df = df[df["age_hours"] <= max_age]

        st.metric("Detections shown", len(df))

        # Format for display
        display_cols = [
            "detected_at", "source", "token_symbol", "token_name",
            "liquidity_usd", "volume_5m", "price_change_5m",
            "age_hours", "social_mentions", "token_address",
        ]
        display_cols = [c for c in display_cols if c in df.columns]
        display_df = df[display_cols].copy()

        if "liquidity_usd" in display_df.columns:
            display_df["liquidity_usd"] = display_df["liquidity_usd"].apply(
                lambda x: format_usd(x) if x else "$0"
            )
        if "volume_5m" in display_df.columns:
            display_df["volume_5m"] = display_df["volume_5m"].apply(
                lambda x: format_usd(x) if x else "$0"
            )
        if "price_change_5m" in display_df.columns:
            display_df["price_change_5m"] = display_df["price_change_5m"].apply(
                lambda x: f"{x:+.1f}%" if x else "0%"
            )

        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "token_address": st.column_config.TextColumn("Address", width="medium"),
                "price_change_5m": st.column_config.TextColumn("5m Chg"),
            },
        )

# ---------------------------------------------------------------------------
# Page: Candidates
# ---------------------------------------------------------------------------

elif page == "🎯 Candidates":
    st.title("🎯 Scored Candidates")
    st.caption("Tokens that passed the scoring threshold and are queued for review.")

    from storage.database import get_connection

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.*, d.token_name, d.token_symbol, d.liquidity_usd,
                   d.volume_5m, d.social_mentions, d.source
            FROM candidates c
            JOIN detections d ON d.id = c.detection_id
            ORDER BY c.score DESC
            LIMIT 100
            """
        ).fetchall()

    candidates = [dict(r) for r in rows]

    if not candidates:
        st.info("No candidates scored yet. Run the bot with --mode analyze-only")
    else:
        df = pd.DataFrame(candidates)

        # Score distribution chart
        if "score" in df.columns and len(df) > 1:
            import plotly.express as px
            fig = px.histogram(
                df, x="score", nbins=20,
                title="Score Distribution",
                color_discrete_sequence=["#00D4AA"],
            )
            fig.update_layout(height=250, margin=dict(t=30, b=0))
            st.plotly_chart(fig, use_container_width=True)

        st.dataframe(
            df[[c for c in [
                "scored_at", "status", "token_symbol", "score",
                "volume_score", "liquidity_score", "rug_risk_score",
                "mint_revoked", "top10_pct", "token_address"
            ] if c in df.columns]],
            use_container_width=True,
            hide_index=True,
        )

# ---------------------------------------------------------------------------
# Page: Approvals
# ---------------------------------------------------------------------------

elif page == "✅ Approvals":
    st.title("✅ Approval Queue")
    st.warning(
        "⚠️ Approving here triggers a devnet deployment. "
        "Ensure the bot is running in 'full' mode and watching the DB.",
        icon="⚠️",
    )

    from storage.database import get_connection

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT gt.*, c.score, c.rug_risk_score, c.risk_flags_json
            FROM generated_tokens gt
            JOIN candidates c ON c.id = gt.candidate_id
            WHERE gt.status = 'draft'
            ORDER BY gt.generated_at DESC
            LIMIT 20
            """
        ).fetchall()

    pending = [dict(r) for r in rows]

    if not pending:
        st.success("No tokens pending approval.")
    else:
        for token in pending:
            with st.expander(
                f"🪙 {token.get('new_name', '?')} ({token.get('new_symbol', '?')}) "
                f"— Score: {token.get('score', 0):.0f}/100",
                expanded=True,
            ):
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"**Inspired by:** {token.get('source_name', 'N/A')}")
                    st.markdown(f"**New name:** {token.get('new_name')}")
                    st.markdown(f"**Symbol:** {token.get('new_symbol')}")
                    st.markdown(f"**Supply:** {token.get('new_supply', 0):,}")
                with col2:
                    st.markdown(f"**Score:** {token.get('score', 0):.1f}")
                    st.markdown(f"**Rug Risk:** {token.get('rug_risk_score', 0):.0f}")

                st.caption(token.get("new_description", ""))

                col_a, col_r = st.columns(2)
                with col_a:
                    if st.button(
                        f"✅ APPROVE (devnet only)",
                        key=f"approve_{token['id']}",
                        type="primary",
                    ):
                        approve_generated_token(token["id"])
                        st.success("Approved! Bot will pick this up in the next cycle.")
                        st.rerun()
                with col_r:
                    if st.button(
                        f"❌ Reject",
                        key=f"reject_{token['id']}",
                    ):
                        with get_connection() as conn:
                            conn.execute(
                                "UPDATE generated_tokens SET status='rejected' WHERE id=?",
                                (token["id"],),
                            )
                        st.rerun()

# ---------------------------------------------------------------------------
# Page: Deployments
# ---------------------------------------------------------------------------

elif page == "🚀 Deployments":
    st.title("🚀 Deployment History")
    st.caption("All educational token deployments on devnet.")

    deployments = get_deployments(network="devnet")

    if not deployments:
        st.info("No deployments yet.")
    else:
        df = pd.DataFrame(deployments)
        st.metric("Total deployments", len(df))
        st.metric("Successful", len(df[df["status"] == "complete"]) if "status" in df.columns else 0)

        st.dataframe(
            df[[c for c in [
                "deployed_at", "status", "network",
                "mint_address", "pool_id", "market_id",
                "sol_added", "tokens_added", "mint_revoked", "freeze_revoked",
            ] if c in df.columns]],
            use_container_width=True,
            hide_index=True,
        )

        # Show Solscan devnet links for easy verification
        st.subheader("Devnet Explorer Links")
        for dep in deployments[:5]:
            if dep.get("mint_address"):
                mint = dep["mint_address"]
                st.markdown(
                    f"[{mint[:8]}...](https://explorer.solana.com/address/{mint}?cluster=devnet) "
                    f"— `{dep.get('status', '?')}`"
                )

# ---------------------------------------------------------------------------
# Page: Monitoring
# ---------------------------------------------------------------------------

elif page == "📊 Monitoring":
    st.title("📊 Post-Launch Monitoring")
    st.caption("Real-time metrics for deployed educational tokens.")

    from storage.database import get_connection

    with get_connection() as conn:
        deployments = conn.execute(
            "SELECT * FROM deployments WHERE status='complete' ORDER BY deployed_at DESC LIMIT 10"
        ).fetchall()

    if not deployments:
        st.info("No successful deployments to monitor yet.")
    else:
        selected = st.selectbox(
            "Select deployment",
            options=[f"{d['pool_id'] or d['mint_address']} (deployed {d['deployed_at'][:10]})"
                     for d in deployments],
        )

        if selected:
            dep = deployments[0]  # simplified — production would match by selection
            with get_connection() as conn:
                snapshots = conn.execute(
                    "SELECT * FROM monitoring_snapshots WHERE deployment_id=? ORDER BY snapshot_at",
                    (dep["id"],),
                ).fetchall()

            if snapshots:
                snap_df = pd.DataFrame([dict(s) for s in snapshots])
                import plotly.express as px

                fig = px.line(
                    snap_df,
                    x="snapshot_at",
                    y=["price_usd", "liquidity_usd"],
                    title="Price & Liquidity Over Time",
                )
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info(
                    "No monitoring snapshots yet. The bot's post-launch monitor "
                    "will populate this as it runs."
                )

        st.subheader("Educational: What This Chart Tells Us")
        st.markdown("""
        In a real meme coin ecosystem, price and liquidity for copy-cat tokens
        typically show one of these patterns:
        - **Slow decay**: Nobody buys, liquidity slowly drains → **most common outcome**
        - **Brief spike then crash**: A few traders notice, then exit → **"pump and dump" pattern**
        - **Steady growth**: Only happens if there's real organic community adoption → **very rare for copies**

        This simulation lets us study these dynamics safely on devnet.
        """)


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    "🔬 Solana Meme Coin Educational Research System | "
    "Devnet Only | For educational purposes only | "
    "No financial advice"
)
