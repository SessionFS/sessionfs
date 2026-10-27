"""External documentation providers (Confluence Cloud in the MVP).

``base`` defines the provider protocol page sync programs against;
``confluence`` is the Confluence Cloud client + Markdown→storage converter;
``templates`` holds the built-in page templates; ``promote`` turns a
SessionFS source (a wiki page or the compiled context) into a previewable
page (deterministic draft, optional BYOK rewrite with citation validation).
"""

from sessionfs.server.integrations.docs.base import (
    DocChildPage,
    DocPage,
    DocSpace,
    DocSpacePage,
    DocumentationError,
    DocumentationNotFound,
    DocumentationProvider,
)
from sessionfs.server.integrations.docs.confluence import ConfluenceClient
from sessionfs.server.integrations.docs.templates import (
    DEFAULT_TEMPLATE,
    SOURCE_FIELDS,
    Template,
    TemplateSection,
    get_template,
    is_builtin_slug,
    list_templates,
)

__all__ = [
    "ConfluenceClient",
    "DEFAULT_TEMPLATE",
    "DocChildPage",
    "DocPage",
    "DocSpace",
    "DocSpacePage",
    "DocumentationError",
    "DocumentationNotFound",
    "DocumentationProvider",
    "SOURCE_FIELDS",
    "Template",
    "TemplateSection",
    "get_template",
    "is_builtin_slug",
    "list_templates",
]
