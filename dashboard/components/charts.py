"""
Reusable Plotly chart components for the Streamlit dashboard.

DISCLAIMER: Educational system for Solana devnet only.
"""

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go


def score_gauge(score: float, title: str = "Score") -> go.Figure:
    """Render a gauge chart for a 0–100 score."""
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=score,
            title={"text": title},
            gauge={
                "axis": {"range": [0, 100]},
                "bar": {"color": "#00D4AA"},
                "steps": [
                    {"range": [0, 40], "color": "#FF6B6B"},
                    {"range": [40, 70], "color": "#FFD93D"},
                    {"range": [70, 100], "color": "#6BCB77"},
                ],
            },
        )
    )
    fig.update_layout(height=200, margin=dict(t=30, b=0, l=20, r=20))
    return fig


def detection_timeline(detections: list[dict]) -> go.Figure:
    """Bar chart of detections per source over time."""
    if not detections:
        return go.Figure()

    df = pd.DataFrame(detections)
    if "detected_at" not in df.columns or "source" not in df.columns:
        return go.Figure()

    df["hour"] = pd.to_datetime(df["detected_at"]).dt.floor("h")
    grouped = df.groupby(["hour", "source"]).size().reset_index(name="count")

    fig = px.bar(
        grouped,
        x="hour",
        y="count",
        color="source",
        title="Detections Per Hour by Source",
        color_discrete_sequence=px.colors.qualitative.Set2,
    )
    fig.update_layout(height=300, margin=dict(t=40, b=0))
    return fig


def liquidity_vs_score(candidates: list[dict]) -> go.Figure:
    """Scatter plot: liquidity vs composite score."""
    if not candidates:
        return go.Figure()

    df = pd.DataFrame(candidates)
    fig = px.scatter(
        df,
        x="liquidity_usd",
        y="score",
        color="rug_risk_score",
        hover_data=["token_symbol", "token_name"],
        title="Liquidity vs. Score (color = rug risk)",
        color_continuous_scale="RdYlGn_r",
        labels={"liquidity_usd": "Liquidity (USD)", "score": "Composite Score"},
    )
    fig.update_layout(height=350, margin=dict(t=40, b=0))
    return fig


def price_history(snapshots: list[dict], symbol: str = "") -> go.Figure:
    """Line chart of price over time for a deployed token."""
    if not snapshots:
        return go.Figure()

    df = pd.DataFrame(snapshots)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["snapshot_at"],
            y=df["price_usd"],
            name="Price (USD)",
            line=dict(color="#00D4AA"),
        )
    )
    fig.update_layout(
        title=f"{symbol} Price History (devnet)",
        height=300,
        margin=dict(t=40, b=0),
    )
    return fig
