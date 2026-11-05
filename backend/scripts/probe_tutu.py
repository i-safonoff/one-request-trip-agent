"""Stage 0: look at what the Tutu MCP server really offers. Run it from a network that reaches mcp.tutu.ru
(Russian IP, no VPN):

    cd backend && python scripts/probe_tutu.py

Writes docs/sources/*.json: the tool list with input schemas, the help resources, and real sample
responses for Moscow / St Petersburg / Tokyo hotels. The transport tools' (search_rail / search_avia /
search_bus) argument names are NOT known yet, so this script only dumps their schemas: use them to
check agents/integrations/transport.py::TutuTransport against real responses, then set TRANSPORT_SOURCE=tutu.
"""
import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.integrations.mcp_client import McpClient, McpError, SourceUnavailable  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "sources"
URL = "https://mcp.tutu.ru/mcp"


def save(name: str, data) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), "utf-8")
    print(f"  saved docs/sources/{name}")


async def main() -> int:
    c = McpClient(URL, timeout=40)
    try:
        await c._ensure()
    except SourceUnavailable as e:
        print(f"Cannot reach {URL}: {e}\nThe server does not answer from this network (it is not reachable from non-Russian IPs).")
        return 1
    print(f"tools: {sorted(c.tools)}")
    save("tools.json", list(c.tools.values()))
    for t in ("search_rail", "search_avia", "search_bus", "search_etrain", "search_multitransport"):
        if t in c.tools:
            print(f"\n{t}: {json.dumps(c.tools[t].get('inputSchema', {}).get('properties', {}), ensure_ascii=False)[:600]}")

    try:
        save("help_overview.json", await c.call("fetch_resource", {"uri": "tutu://help/overview"}))
    except McpError as e:
        print("  help overview failed:", e)

    start = date.today() + timedelta(days=30)
    for city in ("Москва", "Санкт-Петербург", "Токио"):
        args = {"city_name": city, "check_in": str(start), "check_out": str(start + timedelta(days=3)), "adults": 2,
                "page": 1, "page_size": 5, "view": "compact"}
        try:
            res = await c.call("search_hotels", args)
            rows = res.get("hotels", [])
            print(f"\nsearch_hotels {city}: {len(rows)} hotels, resolved_geo={res.get('meta', {}).get('resolved_geo', {}).get('name')}")
            save(f"search_hotels_{city}.json", res)
        except McpError as e:
            print(f"\nsearch_hotels {city}: ERROR {e}")
    await c.aclose()
    print("\nNext: read docs/sources/tools.json, write TutuTransport, run the app with TRANSPORT_SOURCE=tutu.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
