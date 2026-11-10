"""Real local MCP servers for the #7076 tests, each started with the interpreter running the tests."""
import http.server
import os
import socket
import sys
import threading
import textwrap

HEALTHY_SERVER = textwrap.dedent("""
    import os, sys
    from mcp.server.fastmcp import FastMCP
    if os.path.exists(sys.argv[1] + ".broken"):
        sys.stderr.write("broken on purpose\\n")
        sys.exit(1)
    server = FastMCP("healthy")

    @server.tool()
    def echo(text: str) -> str:
        sys.stderr.write(f"echo server log: {text}\\n")
        return text

    @server.tool()
    def fail(text: str) -> str:
        raise ValueError("the tool itself failed")

    @server.tool()
    def crash(text: str) -> str:
        os._exit(1)

    @server.tool()
    def slow(text: str) -> str:
        import time
        time.sleep(60)
        return text

    server.run()
""")


GROWING_SERVER = textwrap.dedent("""
    from mcp.server.fastmcp import Context, FastMCP
    server = FastMCP("growing")

    @server.tool()
    async def grow(text: str, ctx: Context) -> str:
        server.add_tool(lambda: "added", name="added")
        await ctx.session.send_tool_list_changed()
        return "grown"

    server.run()
""")


class UnauthorizedHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(401)
        self.end_headers()

    do_GET = do_POST

    def log_message(self, *args):
        """Keep request logs out of the test output."""


class SilentEventStreamHandler(http.server.BaseHTTPRequestHandler):
    """Answer like an SSE server, then never send the endpoint event the client waits for."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.flush()
        threading.Event().wait(120)

    def log_message(self, *args):
        """Keep request logs out of the test output."""


def serve_silent_event_stream():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SilentEventStreamHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def write_healthy_server(directory):
    path = directory / "healthy_server.py"
    path.write_text(HEALTHY_SERVER)
    return path


def healthy(directory):
    path = write_healthy_server(directory)
    return {"type": "stdio", "command": sys.executable, "args": [str(path), str(path)]}


def growing(directory):
    """A stateful server whose `grow` tool adds a tool and announces tools/list_changed."""
    path = directory / "growing_server.py"
    path.write_text(GROWING_SERVER)
    return {"type": "stdio", "command": sys.executable, "args": [str(path)], "stateful": True}


def break_server(server_conf):
    open(server_conf["args"][1] + ".broken", "w").close()


def repair_server(server_conf):
    os.remove(server_conf["args"][1] + ".broken")


def missing_command():
    return {"type": "stdio", "command": "elitea-test-command-that-does-not-exist", "args": []}


def exits_at_start(stderr_text="boom: missing GITHUB_TOKEN"):
    return {"type": "stdio", "command": sys.executable,
            "args": ["-c", f"import sys; sys.stderr.write({stderr_text!r} + '\\n'); sys.exit(3)"]}


def forks_and_hangs(pid_file):
    """A server that starts a child of its own, as npx or a wrapper script does, and never answers the handshake."""
    script = ("import subprocess, sys, time; child = subprocess.Popen(['sleep', '600']);"
              f"open({str(pid_file)!r}, 'w').write(str(child.pid)); time.sleep(600)")
    return {"type": "stdio", "command": sys.executable, "args": ["-c", script]}


def hangs():
    return {"type": "stdio", "command": sys.executable, "args": ["-c", "import time; time.sleep(120)"]}


def find_closed_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def closed_port_url(transport="http"):
    return {"type": transport, "url": f"http://127.0.0.1:{find_closed_port()}/mcp", "headers": {}}


def serve_unauthorized():
    """Run an HTTP server that answers every request with 401; return it and its address."""
    server = http.server.HTTPServer(("127.0.0.1", 0), UnauthorizedHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
