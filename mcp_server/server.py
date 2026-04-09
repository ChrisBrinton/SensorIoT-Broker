"""SensorIoT MCP Server — read-only tools that proxy the Flask REST API."""

import os
from typing import Optional
import httpx
from mcp.server.fastmcp import FastMCP

API_BASE = os.getenv("API_BASE_URL", "http://rest_server:80")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8080"))

mcp = FastMCP("SensorIoT")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(path: str, params: dict | None = None) -> dict | list | str:
    resp = httpx.get(f"{API_BASE}{path}", params=params, timeout=30)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return resp.text


# ---------------------------------------------------------------------------
# Sensor data tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_stats() -> str:
    """Return the total number of sensor reading documents stored in the database."""
    return _get("/stats")


@mcp.tool()
def get_sensor_list() -> list:
    """Return all distinct node IDs across every gateway."""
    return _get("/sensorlist")


@mcp.tool()
def get_sensor_data(
    node: str,
    period_days: int = 1,
    skip: int = 0,
    sensor_type: str = "",
) -> list:
    """Return historical readings for a single node.

    Args:
        node: The node_id to query.
        period_days: How many days back to retrieve (default 1).
        skip: Skip every N-th record for downsampling (default 0 = no skip).
        sensor_type: Filter by sensor type, e.g. 'F' for Fahrenheit (default = all types).
    """
    return _get(f"/sensor/{node}", {"period": period_days, "skip": skip, "type": sensor_type})


@mcp.tool()
def get_latest(gateway_id: str, period_hours: int = 24) -> list:
    """Return the latest reading for every sensor on a gateway.

    Args:
        gateway_id: The gateway identifier.
        period_hours: Only include sensors active within this many hours (default 24).
    """
    return _get(f"/latest/{gateway_id}", {"period": period_hours})


@mcp.tool()
def get_latests(gateway_ids: list[str], period_hours: int = 1) -> list:
    """Return latest readings across multiple gateways in one call.

    Args:
        gateway_ids: List of gateway identifiers.
        period_hours: Activity window in hours (default 1).
    """
    params = [("gw", gw) for gw in gateway_ids] + [("period", period_hours)]
    resp = httpx.get(f"{API_BASE}/latests", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def get_node_list(gateway_id: str, period_days: int = 1) -> list:
    """Return the list of node IDs that were active on a gateway within a time window.

    Args:
        gateway_id: The gateway identifier.
        period_days: Lookback window in days (default 1).
    """
    return _get(f"/nodelist/{gateway_id}", {"period": period_days})


@mcp.tool()
def get_node_lists(gateway_ids: list[str], period_days: int = 1) -> list:
    """Return active node lists for multiple gateways in one call.

    Args:
        gateway_ids: List of gateway identifiers.
        period_days: Lookback window in days (default 1).
    """
    params = [("gw", gw) for gw in gateway_ids] + [("period", period_days)]
    resp = httpx.get(f"{API_BASE}/nodelists", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def get_gateway_data(
    gateway_id: str,
    nodes: list[str],
    sensor_type: str = "",
    period_days: int = 1,
    timezone: str = "UTC",
) -> list:
    """Return per-node historical data for a gateway, with timezone conversion.

    Args:
        gateway_id: The gateway identifier.
        nodes: List of node_ids to include.
        sensor_type: Filter by type, e.g. 'F' (default = all).
        period_days: How many days back (default 1).
        timezone: IANA timezone string, e.g. 'America/New_York' (default 'UTC').
    """
    params = (
        [("node", n) for n in nodes]
        + [("type", sensor_type), ("period", period_days), ("timezone", timezone)]
    )
    resp = httpx.get(f"{API_BASE}/gw/{gateway_id}", params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Nicknames
# ---------------------------------------------------------------------------

@mcp.tool()
def get_nicknames(gateway_ids: list[str]) -> list:
    """Return sensor and gateway display names for one or more gateways.

    Args:
        gateway_ids: List of gateway identifiers.
    """
    params = [("gw", gw) for gw in gateway_ids]
    resp = httpx.get(f"{API_BASE}/get_nicknames", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# NOAA Forecast
# ---------------------------------------------------------------------------

@mcp.tool()
def get_forecast(
    gateway_id: str,
    node: str = "noaa_forecast",
    sensor_type: str = "F",
    hours_back: int = 0,
) -> list:
    """Return NOAA weather forecast records stored for a gateway.

    Args:
        gateway_id: The gateway identifier.
        node: Node ID for the forecast data (default 'noaa_forecast').
        sensor_type: Sensor type filter (default 'F' = Fahrenheit).
        hours_back: Also include records this many hours into the past (default 0 = future only).
    """
    return _get(f"/forecast/{gateway_id}", {
        "node": node, "type": sensor_type, "hours_back": hours_back,
    })


# ---------------------------------------------------------------------------
# Anomaly Detection
# ---------------------------------------------------------------------------

@mcp.tool()
def get_anomaly_training_status(job_id: str) -> dict:
    """Poll the status of an anomaly model training job.

    Args:
        job_id: UUID returned by the train_anomaly_model endpoint.
    """
    return _get("/training_status", {"job_id": job_id})


@mcp.tool()
def get_anomaly_model_status(gateway_id: str) -> dict:
    """Return metadata for the trained anomaly model of a gateway.

    Args:
        gateway_id: The gateway identifier.
    """
    return _get("/anomaly_model_status", {"gateway_id": gateway_id})


@mcp.tool()
def predict_anomaly(
    gateway_id: str,
    node_id: str,
    period_days: int = 7,
) -> dict:
    """Run anomaly detection on recent data for a sensor node.

    Args:
        gateway_id: The gateway identifier.
        node_id: The node identifier.
        period_days: How many days of data to run the detector over (default 7).
    """
    return _get("/predict_anomaly", {
        "gateway_id": gateway_id, "node_id": node_id, "period": period_days,
    })


# ---------------------------------------------------------------------------
# Regression Forecasting
# ---------------------------------------------------------------------------

@mcp.tool()
def get_regression_training_status(job_id: str) -> dict:
    """Poll the status of a regression model training job.

    Args:
        job_id: UUID returned by the train_regression_model endpoint.
    """
    return _get("/regression_training_status", {"job_id": job_id})


@mcp.tool()
def get_regression_model_status(gateway_id: str) -> dict:
    """Return metadata for all regression models trained for a gateway.

    Args:
        gateway_id: The gateway identifier.
    """
    return _get("/regression_model_status", {"gateway_id": gateway_id})


@mcp.tool()
def get_regression_forecast(
    gateway_id: str,
    node_id: str,
    sensor_type: str = "F",
    hours: int = 48,
) -> dict:
    """Return predicted future sensor values from the regression model.

    Args:
        gateway_id: The gateway identifier.
        node_id: The node identifier.
        sensor_type: Sensor type to forecast (default 'F').
        hours: How many hours ahead to forecast (default 48).
    """
    return _get("/regression_forecast", {
        "gateway_id": gateway_id, "node_id": node_id,
        "type": sensor_type, "hours": hours,
    })


@mcp.tool()
def get_regression_forecast_history(
    gateway_id: str,
    node_id: str,
    sensor_type: str = "F",
    hours_back: int = 48,
) -> dict:
    """Return past stored forecasts for a sensor (for comparison with actuals).

    Args:
        gateway_id: The gateway identifier.
        node_id: The node identifier.
        sensor_type: Sensor type (default 'F').
        hours_back: How far back to retrieve recorded forecasts (default 48).
    """
    return _get("/regression_forecast_history", {
        "gateway_id": gateway_id, "node_id": node_id,
        "type": sensor_type, "hours_back": hours_back,
    })


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

@mcp.tool()
def get_baseline(gateway_id: str, node: str, sensor_type: str = "F") -> list:
    """Return the saved per-hour-of-week baseline buckets for a sensor.

    Args:
        gateway_id: The gateway identifier.
        node: The node identifier.
        sensor_type: Sensor type (default 'F').
    """
    return _get(f"/baseline/{gateway_id}", {"node": node, "type": sensor_type})


@mcp.tool()
def get_baseline_status(
    gateway_id: str,
    node: Optional[str] = None,
    sensor_type: str = "F",
) -> dict:
    """Check whether a baseline exists and when it was last computed.

    Args:
        gateway_id: The gateway identifier.
        node: Node identifier. Omit for a gateway-level existence check.
        sensor_type: Sensor type (default 'F').
    """
    params: dict = {"type": sensor_type}
    if node:
        params["node"] = node
    return _get(f"/baseline_status/{gateway_id}", params)


# ---------------------------------------------------------------------------
# Heatmap / Calendar View
# ---------------------------------------------------------------------------

@mcp.tool()
def get_heatmap(
    gateway_id: str,
    node: str,
    sensor_type: str = "F",
    year: Optional[int] = None,
) -> list:
    """Return daily min/max/avg aggregations for a calendar heatmap view.

    Args:
        gateway_id: The gateway identifier.
        node: The node identifier.
        sensor_type: Sensor type (default 'F').
        year: Year to aggregate (default = current year).
    """
    params: dict = {"node": node, "type": sensor_type}
    if year is not None:
        params["year"] = year
    return _get(f"/heatmap/{gateway_id}", params)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Serves at POST /mcp (streamable-http default path)
    mcp.run(transport="streamable-http", host=MCP_HOST, port=MCP_PORT)
