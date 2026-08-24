"""WordPress tools.

Importing this package registers every WordPress tool on the shared FastMCP
singleton. `tool_registry._MODULE_TO_CATEGORY` maps the `tools.wordpress` module
prefix to `wordpress_action`, and that match is prefix-based, so each submodule
added here lands in the same category automatically.
"""
from . import analytics  # noqa: F401  -- registration side effect
from . import builders  # noqa: F401
from . import commerce  # noqa: F401
from . import core  # noqa: F401
from . import integrations  # noqa: F401
from . import ops  # noqa: F401
from . import seo  # noqa: F401
from . import sites  # noqa: F401
