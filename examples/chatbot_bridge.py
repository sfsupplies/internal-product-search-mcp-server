"""
Client-side bridge for an internal chatbot using the Claude API.

Your chatbot backend (on the LAN) connects to this MCP server (on the LAN),
exposes its tools to Claude, and runs the tool-use loop. The MCP server never
leaves your network; only chatbot <-> Anthropic traffic does. This is the same
pattern the MSSQL MCP server uses, so the chatbot can bridge BOTH servers.

Run:
    pip install anthropic mcp
    export ANTHROPIC_API_KEY=sk-ant-...
    export MCP_URL=http://<vm-host>:8001/mcp
    export MCP_AUTH_TOKEN=<the shared token>
    python examples/chatbot_bridge.py "Find clear Avery translucent vinyl"
"""
import asyncio
import os
import sys

from anthropic import Anthropic
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MCP_URL = os.getenv("MCP_URL", "http://localhost:8001/mcp")
MCP_AUTH_TOKEN = os.environ["MCP_AUTH_TOKEN"]


async def answer(question: str) -> str:
    anthropic = Anthropic()  # reads ANTHROPIC_API_KEY
    headers = {"Authorization": f"Bearer {MCP_AUTH_TOKEN}"}

    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = (await session.list_tools()).tools
            claude_tools = [
                {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
                for t in mcp_tools
            ]

            messages = [{"role": "user", "content": question}]
            while True:
                resp = anthropic.messages.create(
                    model=MODEL, max_tokens=1024, tools=claude_tools, messages=messages
                )
                messages.append({"role": "assistant", "content": resp.content})

                if resp.stop_reason != "tool_use":
                    return "".join(b.text for b in resp.content if b.type == "text")

                tool_results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    result = await session.call_tool(block.name, block.input or {})
                    text = "".join(c.text for c in result.content if c.type == "text")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": text,
                    })
                messages.append({"role": "user", "content": tool_results})


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What Avery vinyl colors are available?"
    print(asyncio.run(answer(q)))
