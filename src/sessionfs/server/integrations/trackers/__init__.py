"""External ticket trackers (Jira Cloud in the MVP).

``base`` defines the provider protocol promotion logic programs against;
``jira`` is the Jira Cloud REST v3 client; ``templates`` holds the built-in
story templates; ``promote`` turns a SessionFS ticket into a previewable
story (deterministic draft, optional BYOK rewrite with citation validation).
"""

from sessionfs.server.integrations.trackers.base import (
    TrackerError,
    TrackerIssue,
    TrackerIssueType,
    TrackerProject,
    TrackerProvider,
)
from sessionfs.server.integrations.trackers.jira import JiraClient
from sessionfs.server.integrations.trackers.templates import (
    DEFAULT_TEMPLATE,
    Template,
    TemplateSection,
    get_template,
    list_templates,
)

__all__ = [
    "DEFAULT_TEMPLATE",
    "JiraClient",
    "Template",
    "TemplateSection",
    "TrackerError",
    "TrackerIssue",
    "TrackerIssueType",
    "TrackerProject",
    "TrackerProvider",
    "get_template",
    "list_templates",
]
