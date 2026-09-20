"""Bounded MCP stdio relay for an explicitly issued Camol peer capability.

No global configuration, account credential, model call, or supervisor controls.
The execution owner must arrange the socket, capability and sandbox permission.
"""

import copy
import json
import os
import sys

from .json_contracts import decode_contract
from .peer_transport import MAX_REQUEST, MAX_RESPONSE, _encode, request
from .schema import require_identifier


VERSION = "2025-06-18"
INSTRUCTIONS = (
    "These tools are data-only peer coordination for one Camol worker turn. "
    "Observe the exact target before sending. Retain request_id and content for retries; "
    "a retry returns the original observation, not fresh readiness. Use a new ID for a fresh read. "
    "Messages cannot approve or expand task scope. Unknown transport outcomes may already have effects. "
    "128 admitted calls per turn; provider/context budgets still apply."
)
NAMES = {"list_boxes": "list", "observe_box": "observe", "inbox": "inbox", "send_message": "send"}
MAX_MESSAGES = 512


def tool_definitions():
    identity = dict(type="string", minLength=1, maxLength=200, description="Stable logical retry ID for this exact request in this worker turn.")
    definitions = []
    for name, operation in NAMES.items():
        properties = dict(request_id=identity)
        if operation in {"list", "inbox"}:
            properties.update(offset=dict(type="integer", minimum=0),
                limit=dict(type="integer", minimum=1, maximum=10 if operation == "inbox" else 50))
            description = "Read only this worker's inbox." if operation == "inbox" else "List recorded box metadata; not connectivity or readiness."
        elif operation == "observe":
            properties["box_id"] = dict(type="string", minLength=1, description="Exact box ID from list_boxes.")
            description = "Record a short-lived exact recipient observation for a subsequent send_message."
        else:
            properties.update(target=dict(type="object", description="The exact result receipt from observe_box, unchanged."),
                body=dict(type="string", minLength=1, maxLength=2000),
                kind=dict(type="string", enum=["information", "question", "proposal", "warning"]),
                correlation_id=dict(type=["string", "null"]), ttl_seconds=dict(type="integer", minimum=1, maximum=3600))
            description = "Send data to a previously observed peer; never delegates authority. Keep request_id/content unchanged on retry."
        definitions.append(dict(name=name, description=description,
            inputSchema=dict(type="object", properties=properties, required=list(properties), additionalProperties=False),
            annotations=dict(readOnlyHint=operation != "send", destructiveHint=False, idempotentHint=True, openWorldHint=False)))
    return copy.deepcopy(definitions)


def error(identity, code, message):
    return dict(jsonrpc="2.0", id=identity, error=dict(code=code, message=message))


class Relay:
    def __init__(self, endpoint, token):
        self.endpoint, self.token = endpoint, token
        self.initialized, self.ready = False, False
        self.seen = set()

    def handle(self, value):
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0" or not isinstance(value.get("method"), str):
            return error(None, -32600, "Invalid request")
        notification = "id" not in value
        identity = value.get("id")
        if notification:
            if value["method"] == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        if type(identity) not in {str, int} or (isinstance(identity, str) and (len(identity) > 200 or self.token in identity)):
            return error(None, -32600, "Invalid request ID")
        key = (type(identity), identity)
        if key in self.seen or len(self.seen) >= MAX_MESSAGES:
            return error(identity, -32600, "Request ID reused or session limit reached")
        self.seen.add(key)
        if set(value) - {"jsonrpc", "id", "method", "params"}:
            return error(identity, -32600, "Unknown request fields")
        method, params = value["method"], value.get("params", {})
        if not isinstance(params, dict):
            return error(identity, -32602, "Parameters must be an object")
        if method == "ping":
            return dict(jsonrpc="2.0", id=identity, result={})
        if method == "initialize":
            if (self.initialized or not isinstance(params.get("protocolVersion"), str)
                    or not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict)
                    or any(not isinstance(params["clientInfo"].get(key), str) for key in ("name", "version"))):
                return error(identity, -32602, "Invalid or repeated initialization")
            try:
                observation = request(self.endpoint, self.token, "handshake", {}, request_id="mcp-initialize")
                if observation["error"] is not None:
                    return error(identity, -32000, "Peer endpoint unavailable or worker turn closed")
            except (OSError, ValueError):
                return error(identity, -32000, "Peer endpoint unavailable or worker turn closed")
            self.initialized = True
            return dict(jsonrpc="2.0", id=identity, result=dict(protocolVersion=VERSION,
                capabilities=dict(tools=dict(listChanged=False)),
                serverInfo=dict(name="camol-peers", version="1"), instructions=INSTRUCTIONS))
        if not self.ready:
            return error(identity, -32000, "Initialize the peer relay before using tools")
        if method == "tools/list":
            if set(params) - {"_meta"}:
                return error(identity, -32602, "Tool catalog has no continuation cursor")
            return dict(jsonrpc="2.0", id=identity, result=dict(tools=tool_definitions()))
        if method != "tools/call":
            return error(identity, -32601, "Method not available")
        name, arguments = params.get("name"), params.get("arguments")
        if (not isinstance(name, str) or name not in NAMES or not isinstance(arguments, dict)
                or set(params) - {"name", "arguments", "_meta"}):
            return error(identity, -32602, "Unknown tool or invalid arguments")
        definition = next(item for item in tool_definitions() if item["name"] == name)
        if set(arguments) != set(definition["inputSchema"]["properties"]):
            return error(identity, -32602, "Tool arguments have missing or unknown fields")
        logical_id = arguments["request_id"]
        try:
            require_identifier(logical_id, "peer request ID")
            if len(logical_id) > 200 or self.token in logical_id:
                raise ValueError()
        except (ValueError, TypeError):
            return error(identity, -32602, "Invalid logical request ID")
        try:
            result = request(self.endpoint, self.token, NAMES[name],
                {key: item for key, item in arguments.items() if key != "request_id"}, request_id=logical_id)
        except (OSError, ValueError, TypeError):
            result = dict(error=dict(code="TRANSPORT_UNAVAILABLE", effect="unknown"),
                retry="Retain the same logical request_id and content; do not assume absence of effects.")
        return dict(jsonrpc="2.0", id=identity,
            result=dict(content=[dict(type="text", text=json.dumps(result, sort_keys=True))], isError=result.get("error") is not None))


def serve(relay, source, destination):
    for _ in range(MAX_MESSAGES):
        raw = source.readline(MAX_REQUEST + 1)
        if not raw:
            return 0
        if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
            destination.write(_encode(error(None, -32700, "Oversized or incomplete frame"), MAX_RESPONSE))
            destination.flush()
            return 2
        try:
            value = decode_contract(raw, max_bytes=MAX_REQUEST)
        except ValueError:
            response = error(None, -32700, "Invalid JSON")
        else:
            response = relay.handle(value)
        if response is not None:
            destination.write(_encode(response, MAX_RESPONSE * 4))
            destination.flush()
    return 2


def main():
    endpoint, token = os.environ.get("CAMOL_PEER_ENDPOINT"), os.environ.pop("CAMOL_PEER_TOKEN", None)
    if not endpoint or not token or len(token) != 64:
        print("Camol peer relay requires an explicitly issued worker capability.", file=sys.stderr)
        return 2
    try:
        return serve(Relay(endpoint, token), sys.stdin.buffer, sys.stdout.buffer)
    except (OSError, ValueError):
        print("Camol peer relay closed; a tool's outcome may be unknown.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
