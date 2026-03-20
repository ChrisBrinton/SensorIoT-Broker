#!/usr/bin/env python3
"""test_sensor.py — Simulated IoT sensor publisher for SensorIoT.

Publishes F (temperature °F), H (humidity %), and optionally P (pressure)
readings for one or more virtual nodes to the SensorIoT MQTT broker at a
configurable interval. Values are random within configurable ranges.

Topic format used:  /GDESGW1/{gateway_id}/{node_id}/{type}/0
This matches the 6-segment schema the DataBroker expects.

Usage examples:
  # Simulate one node on localhost
  python3 test_sensor.py --gateway 140E71 --nodes 1

  # Two nodes, 5-second interval, custom temp range
  python3 test_sensor.py --gateway TESTGW --nodes 1 2 --interval 5 \\
      --temp-range 68 78

  # Remote broker, no pressure readings, slight jitter between nodes
  python3 test_sensor.py --host 192.168.1.10 --gateway GW01 --nodes 1 2 3 \\
      --jitter 0.5
"""

import argparse
import random
import time

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# MQTT topic prefix — must match DataBroker subscription (/GDESGW1/#)
# ---------------------------------------------------------------------------
_TOPIC_PREFIX = "GDESGW1"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Simulated IoT sensor publisher for SensorIoT",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Connection
    p.add_argument("--host", default="localhost", help="MQTT broker hostname or IP")
    p.add_argument("--port", type=int, default=1883, help="MQTT broker port")

    # Identity
    p.add_argument("--gateway", default="TESTGW", help="Gateway ID to publish under")
    p.add_argument(
        "--nodes",
        nargs="+",
        default=["1"],
        metavar="NODE_ID",
        help="One or more node IDs to simulate",
    )

    # Timing
    p.add_argument(
        "--interval",
        type=float,
        default=600.0,
        help="Seconds between publish cycles",
    )
    p.add_argument(
        "--jitter",
        type=float,
        default=0.0,
        help="Max random delay (seconds) added between publishing each node",
    )

    # Temperature range
    p.add_argument(
        "--temp-range",
        nargs=2,
        type=float,
        metavar=("MIN", "MAX"),
        default=[60.0, 90.0],
        help="Temperature range in °F",
    )

    # Humidity range
    p.add_argument(
        "--humidity-range",
        nargs=2,
        type=float,
        metavar=("MIN", "MAX"),
        default=[30.0, 80.0],
        help="Humidity range in %%",
    )

    # Pressure range
    p.add_argument(
        "--pressure-range",
        nargs=2,
        type=float,
        metavar=("MIN", "MAX"),
        default=[29.5, 30.5],
        help="Pressure range in inHg",
    )
    p.add_argument(
        "--no-pressure",
        action="store_true",
        help="Skip publishing pressure (P) readings",
    )

    return p


def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"Connected to MQTT broker at {userdata['host']}:{userdata['port']}")
    else:
        print(f"Connection failed (rc={rc})")


def _publish_node(client, gateway: str, node: str, args: argparse.Namespace) -> None:
    """Publish one F/H/P cycle for a single node."""
    temp_min, temp_max = args.temp_range
    hum_min, hum_max = args.humidity_range
    pres_min, pres_max = args.pressure_range

    readings = [
        ("F", round(random.uniform(temp_min, temp_max), 2)),
        ("H", round(random.uniform(hum_min, hum_max), 2)),
    ]
    if not args.no_pressure:
        readings.append(("P", round(random.uniform(pres_min, pres_max), 3)))

    for sensor_type, value in readings:
        # Trailing /0 makes the topic 6 segments as DataBroker expects
        topic = f"/{_TOPIC_PREFIX}/{gateway}/{node}/{sensor_type}/0"
        client.publish(topic, str(value), qos=0)
        print(f"    {topic}  →  {value}")


def main() -> None:
    args = _build_parser().parse_args()

    client = mqtt.Client()
    client.user_data_set({"host": args.host, "port": args.port})
    client.on_connect = _on_connect

    print(f"Connecting to {args.host}:{args.port} …")
    client.connect(args.host, args.port, keepalive=60)
    client.loop_start()
    time.sleep(1)  # wait for connection

    temp_min, temp_max = args.temp_range
    hum_min, hum_max = args.humidity_range
    pres_min, pres_max = args.pressure_range
    types = "F, H" + ("" if args.no_pressure else ", P")

    print(f"Gateway : {args.gateway}")
    print(f"Nodes   : {', '.join(args.nodes)}")
    print(f"Types   : {types}")
    print(f"Temp    : {temp_min}–{temp_max} °F")
    print(f"Humidity: {hum_min}–{hum_max} %")
    if not args.no_pressure:
        print(f"Pressure: {pres_min}–{pres_max} inHg")
    print(f"Interval: {args.interval}s  Jitter: ±{args.jitter}s")
    print()

    try:
        while True:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]")
            for node in args.nodes:
                _publish_node(client, args.gateway, node, args)
                if args.jitter > 0:
                    time.sleep(random.uniform(0, args.jitter))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
