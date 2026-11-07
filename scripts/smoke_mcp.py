"""End-to-end smoke test: start the MCP server over stdio and call its tools like Claude would."""
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "ops_sentinel.server"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", [t.name for t in tools.tools])
            for name, args in [("run_checks", {}), ("get_kpis", {}),
                               ("list_issues", {"severity": "critical", "limit": 2}),
                               ("explain_issue", {"issue_id": None}),
                               ("owner_breakdown", {})]:
                if name == "explain_issue":
                    args = {"issue_id": first_id}
                res = await s.call_tool(name, args)
                text = res.content[0].text if res.content else json.dumps(res.structured_content)
                if name == "list_issues":
                    data = res.structured_content or json.loads(text)
                    data = data.get("result", data) if isinstance(data, dict) else data
                    first_id = data[0]["issue_id"]
                print(f"\n== {name} ==\n{text[:600]}")


asyncio.run(main())
