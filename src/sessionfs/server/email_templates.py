"""HTML email templates for transactional emails."""

from __future__ import annotations


def handoff_email(
    *,
    sender_email: str,
    session_title: str | None,
    source_tool: str | None,
    model_id: str | None,
    message_count: int,
    total_tokens: int,
    git_remote: str | None,
    git_branch: str | None,
    sender_message: str | None,
    handoff_id: str,
    pull_command: str,
    dashboard_url: str | None = None,
    trust_score: float | None = None,
) -> str:
    """Generate HTML email for a session handoff notification."""
    title_display = session_title or "Untitled session"
    tool_display = source_tool or "Unknown tool"
    model_display = model_id or "Unknown model"
    token_display = f"{total_tokens:,}" if total_tokens else "N/A"

    git_html = ""
    if git_remote:
        branch_part = f" ({git_branch})" if git_branch else ""
        git_html = (
            f"<tr><td style='padding: 6px 12px; color: #8b949e;'>Repository</td>"
            f"<td style='padding: 6px 12px; color: #e6edf3;'>{git_remote}{branch_part}</td></tr>"
        )

    audit_html = ""
    if trust_score is not None:
        score_pct = f"{trust_score:.0%}"
        audit_html = (
            f"<tr><td style='padding: 6px 12px; color: #8b949e;'>Audit Score</td>"
            f"<td style='padding: 6px 12px; color: #e6edf3;'>{score_pct}</td></tr>"
        )

    message_html = ""
    if sender_message:
        message_html = (
            "<div style='background: #1c2128; border-left: 3px solid #4f9cf7; "
            "padding: 12px 16px; margin: 16px 0; border-radius: 4px;'>"
            f"<p style='color: #8b949e; margin: 0 0 8px 0; font-size: 12px;'>Message from {sender_email}:</p>"
            f"<p style='color: #e6edf3; margin: 0;'>{sender_message}</p>"
            "</div>"
        )

    dashboard_html = ""
    if dashboard_url:
        dashboard_html = (
            f"<p style='margin-top: 12px;'><a href='{dashboard_url}' "
            "style='color: #4f9cf7; text-decoration: none;'>View in dashboard</a></p>"
        )

    logo_svg = (
        "<svg viewBox='0 0 180 50' width='140' xmlns='http://www.w3.org/2000/svg'>"
        "<path d='M22,8 Q14,8 14,16 L14,34 Q14,42 22,42' fill='none' stroke='#4f9cf7' stroke-width='3' stroke-linecap='round'/>"
        "<path d='M42,8 Q50,8 50,16 L50,34 Q50,42 42,42' fill='none' stroke='#3ddc84' stroke-width='3' stroke-linecap='round'/>"
        "<circle cx='32' cy='25' r='5' fill='#4f9cf7'/>"
        "<text x='60' y='31' font-family='-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif' font-size='19' font-weight='500' fill='#e6edf3' letter-spacing='-0.5'>Session<tspan fill='#4f9cf7'>FS</tspan></text>"
        "</svg>"
    )

    return (
        "<div style='font-family: system-ui, -apple-system, sans-serif; max-width: 560px; "
        "margin: 0 auto; background: #0a0c10; color: #e6edf3; padding: 32px; "
        "border-radius: 8px;'>"
        # Logo
        f"<div style='margin-bottom: 20px;'>{logo_svg}</div>"
        # Header
        "<div style='margin-bottom: 24px;'>"
        "<h2 style='margin: 0 0 8px 0; color: #e6edf3;'>Session handoff</h2>"
        f"<p style='margin: 0; color: #8b949e;'>{sender_email} shared a session with you</p>"
        "</div>"
        # Message from sender
        f"{message_html}"
        # Session details table
        "<table style='width: 100%; border-collapse: collapse; margin: 16px 0; "
        "background: #161b22; border-radius: 6px; overflow: hidden;'>"
        f"<tr><td style='padding: 6px 12px; color: #8b949e;'>Session</td>"
        f"<td style='padding: 6px 12px; color: #e6edf3; font-weight: 500;'>{title_display}</td></tr>"
        f"<tr><td style='padding: 6px 12px; color: #8b949e;'>Tool</td>"
        f"<td style='padding: 6px 12px; color: #e6edf3;'>{tool_display}</td></tr>"
        f"<tr><td style='padding: 6px 12px; color: #8b949e;'>Model</td>"
        f"<td style='padding: 6px 12px; color: #e6edf3;'>{model_display}</td></tr>"
        f"<tr><td style='padding: 6px 12px; color: #8b949e;'>Messages</td>"
        f"<td style='padding: 6px 12px; color: #e6edf3;'>{message_count}</td></tr>"
        f"<tr><td style='padding: 6px 12px; color: #8b949e;'>Tokens</td>"
        f"<td style='padding: 6px 12px; color: #e6edf3;'>{token_display}</td></tr>"
        f"{git_html}"
        f"{audit_html}"
        "</table>"
        # Pull instructions
        "<div style='background: #161b22; padding: 16px; border-radius: 6px; margin: 16px 0;'>"
        "<p style='color: #8b949e; margin: 0 0 8px 0; font-size: 13px;'>Pull this session:</p>"
        f"<code style='display: block; background: #0a0c10; color: #4f9cf7; padding: 10px 14px; "
        f"border-radius: 4px; font-size: 13px; word-break: break-all;'>{pull_command}</code>"
        "</div>"
        f"{dashboard_html}"
        # Footer
        "<p style='color: #484f58; font-size: 12px; margin-top: 24px; border-top: 1px solid #21262d; "
        "padding-top: 16px;'>Sent by SessionFS. "
        "If you didn't expect this email, you can safely ignore it.</p>"
        "</div>"
    )
