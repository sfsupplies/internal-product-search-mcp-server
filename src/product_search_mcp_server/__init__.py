from . import server


def main():
    """Main entry point. server.main() runs stdio or uvicorn based on
    MCP_TRANSPORT."""
    server.main()


__all__ = ["main", "server"]
