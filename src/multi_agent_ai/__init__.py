"""Multi-Agent AI: native Windows multi-agent development orchestrator.

This package currently implements the protocol/state foundation only: typed models and a
parser/writer for the coordination files in docs/protocol/formats.md (format 1), a
configurable state root with a derived SQLite index, and the ``init``/``index``/``inspect``
CLI. Agent execution, provider adapters, routing and VS Code integration are not
implemented.
"""

__version__ = "0.1.0.dev0"
