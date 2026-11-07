"""Start the MCP server over stdio and call its tools the way Claude would."""
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
            print("tools:", [t.name for t in (await s.list_tools()).tools])
            for name, args in [("run_checks", {}), ("list_issues", {"severity": "critical", "limit": 2}),
                               ("explain_issue", {"issue_id": "UNAPPLIED_CASH:REM-0003"}),
                               ("lookup", {"ref": "C0107"}), ("partner_ageing", {})]:
                res = await s.call_tool(name, args)
                text = res.content[0].text if res.content else json.dumps(res.structured_content)
                print(f"\n== {name} {args} ==\n{text[:500]}")


asyncio.run(main())
