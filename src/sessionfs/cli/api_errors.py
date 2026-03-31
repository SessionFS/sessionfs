"""Handle API errors with tier-aware upgrade prompts."""

from __future__ import annotations

from sessionfs.cli.common import console, err_console


def handle_api_response(resp) -> None:
    """Check an httpx response for tier/role errors and print friendly messages.

    Call this after every cloud API call. If the response indicates an
    upgrade_required or insufficient_role error, prints a user-friendly
    message and exits. For other errors, prints the error and exits.
    """
    if resp.status_code == 403:
        try:
            detail = resp.json().get("detail", {})
        except Exception:
            detail = {}

        if isinstance(detail, dict):
            error_type = detail.get("error", "")

            if error_type == "upgrade_required":
                required = detail.get("required_tier", "a higher tier")
                current = detail.get("current_tier", "free")
                url = detail.get("upgrade_url", "https://sessionfs.dev/pricing")

                console.print(f"\n[yellow]This feature requires {required} tier.[/yellow]")
                console.print(f"  Current tier: {current}")
                console.print(f"\n  Upgrade at: [link={url}]{url}[/link]")
                raise SystemExit(0)

            if error_type == "storage_limit":
                limit = detail.get("limit", 0)
                current = detail.get("current_usage", 0)
                url = detail.get("upgrade_url", "https://sessionfs.dev/pricing")

                console.print(f"\n[yellow]Storage limit reached ({_fmt(limit)}).[/yellow]")
                console.print(f"  Current usage: {_fmt(current)}")
                console.print(f"\n  Upgrade at: [link={url}]{url}[/link]")
                raise SystemExit(0)

            if error_type == "insufficient_role":
                required = detail.get("required_role", "admin")
                current = detail.get("current_role", "member")

                console.print(f"\n[yellow]This action requires the {required} role.[/yellow]")
                console.print(f"  Your role: {current}")
                raise SystemExit(0)

            if error_type == "seat_limit":
                console.print("\n[yellow]All seats are in use.[/yellow]")
                console.print("  Upgrade your plan for more seats.")
                raise SystemExit(0)

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        err_console.print(f"[red]API error ({resp.status_code}): {detail}[/red]")
        raise SystemExit(1)


def _fmt(n: int) -> str:
    """Human-readable bytes."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
