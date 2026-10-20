"""Safe hosted ingestion for portable Agent Skill and Agent Plugin packages.

The validation itself lives in :mod:`sessionfs.skills.publish_validation` so
the CLI (`sfs skill validate` / `sfs skill publish`) runs exactly the same
checks as the server. This module keeps the server-side import path stable.
"""

from __future__ import annotations

from sessionfs.skills.package import scan_mcp_tree
from sessionfs.skills.publish_validation import (
    _BINARY_MAGIC,
    _EXECUTABLE_PARTS,
    _EXTENDED_HEADER_TYPES,
    _INSTRUCTION_SUFFIXES,
    _WINDOWS_RESERVED,
    CODE_INVALID_PACKAGE,
    CODE_LICENSE_REQUIRED,
    LICENSE_REQUIRED_MESSAGE,
    MAX_ARCHIVE_MEMBERS,
    MAX_DECOMPRESSED_BYTES,
    MAX_DLP_TEXT_BYTES,
    MAX_EXTENDED_HEADER_BYTES,
    MAX_INSTRUCTION_BYTES,
    MAX_MEMBER_BYTES,
    MAX_PACKAGE_BYTES,
    MAX_PACKAGE_FILES,
    MAX_UPLOAD_BYTES,
    HostedSkillPackage,
    HostedSkillPackageError,
    PackageValidationError,
    _BudgetedReader,
    _canonical_archive,
    _extract_archive,
    _HeaderGuardReader,
    _instruction_bundle,
    _is_text_member,
    _looks_binary,
    _safe_member_name,
    _scan_package_dlp,
    check_publish_license,
    validate_hosted_package,
)

__all__ = [
    "CODE_INVALID_PACKAGE",
    "CODE_LICENSE_REQUIRED",
    "LICENSE_REQUIRED_MESSAGE",
    "MAX_ARCHIVE_MEMBERS",
    "MAX_DECOMPRESSED_BYTES",
    "MAX_DLP_TEXT_BYTES",
    "MAX_EXTENDED_HEADER_BYTES",
    "MAX_INSTRUCTION_BYTES",
    "MAX_MEMBER_BYTES",
    "MAX_PACKAGE_BYTES",
    "MAX_PACKAGE_FILES",
    "MAX_UPLOAD_BYTES",
    "HostedSkillPackage",
    "HostedSkillPackageError",
    "PackageValidationError",
    "_BINARY_MAGIC",
    "_EXECUTABLE_PARTS",
    "_EXTENDED_HEADER_TYPES",
    "_INSTRUCTION_SUFFIXES",
    "_WINDOWS_RESERVED",
    "_BudgetedReader",
    "_HeaderGuardReader",
    "_canonical_archive",
    "_extract_archive",
    "_instruction_bundle",
    "_is_text_member",
    "_looks_binary",
    "_safe_member_name",
    "_scan_package_dlp",
    "check_publish_license",
    "scan_mcp_tree",
    "validate_hosted_package",
]
