"""fitness-hub: local-first fitness data hub (core library).

The ``fitness_hub`` package is shared by two distributions: ``fitness-hub-core``
(this one: config, storage, providers, sync, queries) and ``fitness-hub-mcp``
(``fitness_hub.cli`` and ``fitness_hub.mcp_server``). ``extend_path`` lets the
second distribution add modules to this package even when both are installed
in editable mode from separate checkouts.
"""

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)

__version__ = "0.1.0"
