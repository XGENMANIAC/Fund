"""
Streamlit Dashboard — Meme Coin Educational Detection System (v2)

DISCLAIMER: Educational system for Solana devnet only.
This dashboard is for monitoring and approving educational deployments.
Do NOT use for real trading decisions.

Run with: streamlit run dashboard/app.py

Features (v2):
  - Live detection feed with filtering
  - Candidate queue with scores and risk flags
  - Approval queue (approve/reject from UI)
  - 🪞 Side-by-side Original vs Clone comparison with image previews + diff
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
    get_variation_log,
    initialize_db,
    get_connection,
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
        [
            "📡 Live Detections",
            "🎯 Candidates",
            "✅ Approvals",
            "🪞 Comparison",
            "🚀 Deployments",
            "📊 Monitoring",
        ],
        index=0,
    )

    cfg = load_config()
    st.divider()
    st.caption(f"Network: **{cfg.get('network', 'devnet').upper()}**")
    st.caption("Mode: **educational**")
    st.caption(
        f"Approval gate: **{'ENABLED' if cfg.get('approval', {}).get('enabled', True) else 'DISABLED'}**"
    )

    refresh = st.button("🔄 Refresh Data")
    auto_refresh = st.toggle("Auto-refresh (15s)", value=False)

# ---------------------------------------------------------------------------
# Auto-refresh
# ---------------------------------------------------------------------------

if auto_refresh:
    import time
    refresh_interval = cfg.get("dashboard", {}).get("auto_refresh_seconds", 15)
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
    st.caption("Tokens detected across DexScreener, pump.fun, Birdeye, and social monitors.")

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

        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Detections shown", len(df))
        with_image = df["image_uri"].notna().sum() if "image_uri" in df.columns else 0
        col_b.metric("With image", int(with_image))
        with_desc = df["original_description"].notna().sum() if "original_description" in df.columns else 0
        col_c.metric("With description", int(with_desc))

        # Format for display
        display_cols = [
            "detected_at", "source", "token_symbol", "token_name",
            "liquidity_usd", "volume_5m", "price_change_5m",
            "age_hours", "social_mentions",
            "image_uri", "metadata_uri", "token_address",
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
                "image_uri": st.column_config.LinkColumn("Image", display_text="🖼"),
                "metadata_uri": st.column_config.LinkColumn("Metadata", display_text="📄"),
            },
        )

# ---------------------------------------------------------------------------
# Page: Candidates
# ---------------------------------------------------------------------------

elif page == "🎯 Candidates":
    st.title("🎯 Scored Candidates")
    st.caption("Tokens that passed the scoring threshold.")

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.*, d.token_name, d.token_symbol, d.liquidity_usd,
                   d.volume_5m, d.social_mentions, d.source,
                   d.image_uri, d.original_description
            FROM candidates c
            JOIN detections d ON d.id = c.detection_id
            ORDER BY c.score DESC
            LIMIT 100
            """
        ).fetchall()

    candidates = [dict(r) for r in rows]

    if not candidates:
        st.info("No candidates scored yet. Run: `python -m bot.main --mode analyze-only`")
    else:
        df = pd.DataFrame(candidates)

        # Dual score distribution
        col1, col2 = st.columns(2)
        with col1:
            if "score" in df.columns and len(df) > 1:
                import plotly.express as px
                fig = px.histogram(
                    df, x="score", nbins=20,
                    title="Virality Score Distribution",
                    color_discrete_sequence=["#00D4AA"],
                )
                fig.update_layout(height=200, margin=dict(t=30, b=0))
                st.plotly_chart(fig, use_container_width=True)

        with col2:
            if "copy_viability" in df.columns and len(df) > 1:
                import plotly.express as px
                fig = px.histogram(
                    df, x="copy_viability", nbins=20,
                    title="Copy Viability Score Distribution",
                    color_discrete_sequence=["#FFD700"],
                )
                fig.update_layout(height=200, margin=dict(t=30, b=0))
                st.plotly_chart(fig, use_container_width=True)

        st.dataframe(
            df[[c for c in [
                "scored_at", "status", "token_symbol", "score", "copy_viability",
                "volume_score", "liquidity_score", "rug_risk_score",
                "has_image", "has_description", "has_metadata_uri",
                "mint_revoked", "top10_pct", "token_address",
            ] if c in df.columns]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "score": st.column_config.ProgressColumn("Score", max_value=100),
                "copy_viability": st.column_config.ProgressColumn("Copy Score", max_value=100),
            },
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

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT gt.*, c.score, c.rug_risk_score, c.copy_viability
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
                f"— Virality: {token.get('score', 0):.0f} | Copy: {token.get('copy_viability', 0):.0f}",
                expanded=True,
            ):
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"**Clone of:** {token.get('source_name', 'N/A')} ({token.get('source_symbol', '?')})")
                    st.markdown(f"**New name:** `{token.get('new_name')}`")
                    st.markdown(f"**Symbol:** `{token.get('new_symbol')}`")
                    st.markdown(f"**Supply:** {token.get('new_supply', 0):,}")
                    if token.get("mimicry_score"):
                        st.markdown(f"**Mimicry score:** {token['mimicry_score']:.0f}%")
                    if token.get("variation_rule"):
                        st.caption(f"Rules: `{token['variation_rule']}`")
                with col2:
                    st.markdown(f"**Virality score:** {token.get('score', 0):.1f}/100")
                    st.markdown(f"**Copy viability:** {token.get('copy_viability', 0):.1f}/100")
                    st.markdown(f"**Rug Risk:** {token.get('rug_risk_score', 0):.0f}/100")
                    if token.get("name_diff"):
                        st.code(f"Name:   {token['name_diff']}\nSymbol: {token.get('symbol_diff', '')}")

                st.caption(token.get("new_description", ""))

                col_a, col_r = st.columns(2)
                with col_a:
                    if st.button(
                        "✅ APPROVE (devnet only)",
                        key=f"approve_{token['id']}",
                        type="primary",
                    ):
                        approve_generated_token(token["id"])
                        st.success("Approved! Bot will pick this up in the next cycle.")
                        st.rerun()
                with col_r:
                    if st.button("❌ Reject", key=f"reject_{token['id']}"):
                        with get_connection() as conn:
                            conn.execute(
                                "UPDATE generated_tokens SET status='rejected' WHERE id=?",
                                (token["id"],),
                            )
                        st.rerun()

# ---------------------------------------------------------------------------
# Page: Side-by-side Comparison (v2 — new page)
# ---------------------------------------------------------------------------

elif page == "🪞 Comparison":
    st.title("🪞 Original vs Clone Comparison")
    st.caption(
        "Side-by-side comparison of the original detected token and its educational clone, "
        "with mimicry diff and variation rule audit log."
    )

    with get_connection() as conn:
        tokens = conn.execute(
            """
            SELECT gt.id, gt.source_name, gt.source_symbol, gt.new_name, gt.new_symbol,
                   gt.source_description, gt.new_description,
                   gt.source_image_uri, gt.image_uri, gt.uploaded_image_uri,
                   gt.name_diff, gt.symbol_diff, gt.description_diff,
                   gt.variation_rule, gt.mimicry_score,
                   gt.generated_at, gt.status,
                   c.score, c.copy_viability
            FROM generated_tokens gt
            JOIN candidates c ON c.id = gt.candidate_id
            ORDER BY gt.generated_at DESC
            LIMIT 50
            """
        ).fetchall()

    tokens = [dict(t) for t in tokens]

    if not tokens:
        st.info("No generated tokens yet. Run the bot to produce candidates.")
    else:
        # Token selector
        labels = [
            f"{t['source_symbol']} → {t['new_symbol']} ({t['generated_at'][:10]}) [{t['status']}]"
            for t in tokens
        ]
        selected_idx = st.selectbox("Select token pair", range(len(labels)), format_func=lambda i: labels[i])
        token = tokens[selected_idx]

        # Mimicry score gauge
        mimicry_score = token.get("mimicry_score") or 0.0
        col_gauge, col_info = st.columns([1, 3])
        with col_gauge:
            from dashboard.components.charts import score_gauge
            st.plotly_chart(score_gauge(mimicry_score, "Mimicry %"), use_container_width=True)
        with col_info:
            st.markdown(f"**Variation rule:** `{token.get('variation_rule', 'N/A')}`")
            st.markdown(f"**Virality score:** {token.get('score', 0):.1f}/100")
            st.markdown(f"**Copy viability:** {token.get('copy_viability', 0):.1f}/100")
            st.markdown(f"**Status:** `{token.get('status', '?')}`")

        st.divider()

        # Side-by-side layout
        left, right = st.columns(2)

        # --- ORIGINAL ---
        with left:
            st.subheader(f"🔵 Original: {token['source_name']} ({token['source_symbol']})")

            src_image = token.get("source_image_uri", "")
            if src_image and src_image.startswith("http"):
                try:
                    st.image(src_image, caption="Original token image", width=200)
                except Exception:
                    st.caption(f"Image: {src_image}")
            else:
                st.caption("No image available")

            src_desc = token.get("source_description", "")
            if src_desc:
                st.markdown("**Description:**")
                st.text_area(
                    "Original description",
                    value=src_desc,
                    height=120,
                    disabled=True,
                    label_visibility="collapsed",
                )
            else:
                st.caption("No description available")

        # --- CLONE ---
        with right:
            st.subheader(f"🟢 Clone: {token['new_name']} ({token['new_symbol']})")

            clone_image = token.get("uploaded_image_uri") or token.get("image_uri", "")
            if clone_image and clone_image.startswith("http"):
                try:
                    st.image(clone_image, caption="Clone token image (re-uploaded)", width=200)
                except Exception:
                    st.caption(f"Image: {clone_image}")
            else:
                st.caption("No image (will use placeholder)")

            new_desc = token.get("new_description", "")
            if new_desc:
                st.markdown("**Description:**")
                st.text_area(
                    "Clone description",
                    value=new_desc,
                    height=120,
                    disabled=True,
                    label_visibility="collapsed",
                )
            else:
                st.caption("No description generated")

        st.divider()

        # Diff view
        st.subheader("Diff Summary")
        diff_data = {
            "Field": ["Name", "Symbol", "Description rule"],
            "Original": [
                token.get("source_name", ""),
                token.get("source_symbol", ""),
                "(source text)",
            ],
            "Clone": [
                token.get("new_name", ""),
                token.get("new_symbol", ""),
                token.get("description_diff", ""),
            ],
            "Diff": [
                token.get("name_diff", ""),
                token.get("symbol_diff", ""),
                token.get("variation_rule", ""),
            ],
        }
        st.dataframe(pd.DataFrame(diff_data), use_container_width=True, hide_index=True)

        # Variation audit log
        st.subheader("Variation Rule Audit Log")
        variation_log = get_variation_log(token["id"])
        if variation_log:
            vdf = pd.DataFrame(variation_log)
            st.dataframe(
                vdf[[c for c in [
                    "logged_at", "field", "rule_name",
                    "original_value", "modified_value", "similarity_pct",
                ] if c in vdf.columns]],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "similarity_pct": st.column_config.ProgressColumn(
                        "Similarity %", max_value=100
                    ),
                },
            )
        else:
            st.caption("No variation log entries yet for this token.")

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
        col1, col2 = st.columns(2)
        col1.metric("Total deployments", len(df))
        col2.metric(
            "Successful",
            len(df[df["status"] == "complete"]) if "status" in df.columns else 0,
        )

        st.dataframe(
            df[[c for c in [
                "deployed_at", "status", "network",
                "mint_address", "pool_id", "market_id",
                "sol_added", "tokens_added", "mint_revoked", "freeze_revoked",
            ] if c in df.columns]],
            use_container_width=True,
            hide_index=True,
        )

        st.subheader("Devnet Explorer Links")
        for dep in deployments[:5]:
            if dep.get("mint_address"):
                mint = dep["mint_address"]
                st.markdown(
                    f"[{mint[:8]}...](https://explorer.solana.com/address/{mint}?cluster=devnet) "
                    f"— `{dep.get('status', '?')}`"
                )

# ---------------------------------------------------------------------------
# Page: Post-Launch Monitoring
# ---------------------------------------------------------------------------

elif page == "📊 Monitoring":
    st.title("📊 Post-Launch Monitoring")
    st.caption("Real-time metrics for deployed educational tokens on devnet.")

    with get_connection() as conn:
        deployments = conn.execute(
            """
            SELECT d.*, gt.new_name, gt.new_symbol, gt.source_name
            FROM deployments d
            JOIN generated_tokens gt ON gt.id = d.generated_token_id
            WHERE d.status='complete'
            ORDER BY d.deployed_at DESC
            LIMIT 10
            """
        ).fetchall()

    if not deployments:
        st.info("No successful deployments to monitor yet.")
    else:
        dep_labels = [
            f"{d['new_symbol']} (clone of {d['source_name']}) — {d['deployed_at'][:10]}"
            for d in deployments
        ]
        selected_idx = st.selectbox(
            "Select deployment",
            range(len(dep_labels)),
            format_func=lambda i: dep_labels[i],
        )
        dep = deployments[selected_idx]

        with get_connection() as conn:
            snapshots = conn.execute(
                "SELECT * FROM monitoring_snapshots WHERE deployment_id=? ORDER BY snapshot_at",
                (dep["id"],),
            ).fetchall()

        snap_list = [dict(s) for s in snapshots]

        col1, col2, col3 = st.columns(3)
        if snap_list:
            latest = snap_list[-1]
            col1.metric("Latest Price", f"${latest.get('price_usd', 0):.6f}")
            col2.metric("Liquidity", format_usd(latest.get("liquidity_usd", 0)))
            col3.metric("1h Volume", format_usd(latest.get("volume_1h", 0)))

            snap_df = pd.DataFrame(snap_list)
            import plotly.express as px
            import plotly.graph_objects as go

            # Price + liquidity dual-axis chart
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=snap_df["snapshot_at"], y=snap_df["price_usd"],
                name="Price (USD)", line=dict(color="#00D4AA"),
            ))
            fig.add_trace(go.Scatter(
                x=snap_df["snapshot_at"], y=snap_df["liquidity_usd"],
                name="Liquidity (USD)", line=dict(color="#FFD700"),
                yaxis="y2",
            ))
            fig.update_layout(
                title=f"{dep['new_symbol']} — Price & Liquidity (devnet)",
                yaxis=dict(title="Price (USD)"),
                yaxis2=dict(title="Liquidity (USD)", overlaying="y", side="right"),
                height=350,
                margin=dict(t=40, b=0),
            )
            st.plotly_chart(fig, use_container_width=True)

            # Volume + holders
            if "volume_1h" in snap_df.columns and "holders" in snap_df.columns:
                fig2 = px.bar(
                    snap_df,
                    x="snapshot_at",
                    y="volume_1h",
                    title="1h Volume Over Time",
                    color_discrete_sequence=["#6BCB77"],
                )
                st.plotly_chart(fig2, use_container_width=True)
        else:
            col1.metric("Snapshots", 0)
            st.info(
                "No monitoring snapshots yet. "
                "The bot's post-launch monitor will populate this as it runs."
            )

        st.subheader("Educational: What This Chart Tells Us")
        st.markdown("""
        In a real meme coin ecosystem, price and liquidity for copy-cat tokens
        typically show one of these patterns:
        - **Slow decay**: Nobody buys, liquidity slowly drains → **most common outcome**
        - **Brief spike then crash**: A few traders notice, then exit → **"pump and dump" pattern**
        - **Steady growth**: Only if there is real organic adoption → **very rare for copies**

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
