"""Thin deterministic JSON adapter for the public collaborative-terminal library."""

import asyncio
import json

from .cli import _EnvelopeArgumentParser
from .terminal import TerminalError, TerminalFleet

OPERATIONS = (
    "fleet",
    "state",
    "capture",
    "save_view",
    "operation",
    "acknowledge_operation",
    "authorize_input",
    "revoke_input",
    "input",
    "manage",
    "attach",
    "output",
    "detach",
)


def main(argv=None):
    parser = _EnvelopeArgumentParser(
        description="Deterministic collaborative tmux actions. Explicit socket scope; no model calls. Reads are default; input/management need host flags and per-action authority. Request receipts prevent automatic replay. Use tmux-fleet-mcp for a retained interactive terminal viewer."
    )
    parser.add_argument(
        "--storage", required=True, help="Private retained receipts/view directory."
    )
    parser.add_argument("--socket-dir")
    parser.add_argument("--socket-name")
    parser.add_argument("--allow-input", action="store_true")
    parser.add_argument("--allow-management", action="store_true")
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument(
        "--arguments",
        default="{}",
        help="JSON keyword arguments for the identically named TerminalFleet library method; method schemas are also exposed by MCP tools.",
    )
    args = parser.parse_args(argv)

    async def run():
        library = TerminalFleet(
            args.storage,
            socket_dir=args.socket_dir,
            socket_name=args.socket_name,
            allow_input=args.allow_input,
            allow_management=args.allow_management,
        )
        try:
            result = await getattr(library, args.operation)(
                **json.loads(args.arguments)
            )
            print(json.dumps(result))
            if args.operation in {"input", "manage", "authorize_input"} and result.get(
                "status"
            ) in {
                "refused",
                "unknown",
            }:
                raise SystemExit(2)
        except (TerminalError, TypeError, ValueError) as error:
            print(
                json.dumps(
                    {
                        "error": error.as_dict()
                        if isinstance(error, TerminalError)
                        else {
                            "code": "INVALID_INPUT",
                            "message": str(error),
                            "remedy": "Pass the documented library arguments as a JSON object.",
                        }
                    }
                )
            )
            raise SystemExit(2)
        finally:
            await library.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
