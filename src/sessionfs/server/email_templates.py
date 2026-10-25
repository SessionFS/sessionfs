"""HTML email templates for transactional emails."""

from __future__ import annotations

import html as _html


def _shell() -> str:
    """Outer wrapper used by all v0.10.9 lifecycle email templates."""
    return (
        "<div style='font-family: system-ui, -apple-system, sans-serif; max-width: 560px; "
        "margin: 0 auto; background: #0a0c10; color: #e6edf3; padding: 32px; "
        "border-radius: 8px;'>"
    )


def _footer() -> str:
    return (
        "<p style='color: #484f58; font-size: 12px; margin-top: 24px; border-top: 1px solid #21262d; "
        "padding-top: 16px;'>Sent by SessionFS. "
        "If you didn't expect this email, you can safely ignore it.</p>"
        "</div>"
    )


def _quote_block(label: str, content: str) -> str:
    safe = _html.escape(content)
    safe_label = _html.escape(label)
    return (
        "<div style='background: #1c2128; border-left: 3px solid #4f9cf7; "
        "padding: 12px 16px; margin: 16px 0; border-radius: 4px;'>"
        f"<p style='color: #8b949e; margin: 0 0 8px 0; font-size: 12px;'>{safe_label}</p>"
        f"<p style='color: #e6edf3; margin: 0;'>{safe}</p>"
        "</div>"
    )


def handoff_claimed_email(
    *,
    recipient_email: str,
    session_title: str | None,
    handoff_id: str,
) -> str:
    """Email sent to the sender when their handoff is claimed."""
    title = _html.escape(session_title or "Untitled session")
    recipient = _html.escape(recipient_email)
    hid = _html.escape(handoff_id)
    return (
        _shell()
        + "<h2 style='margin: 0 0 8px 0; color: #e6edf3;'>Handoff claimed</h2>"
        + f"<p style='color: #8b949e;'>{recipient} claimed your handoff <strong>{title}</strong>.</p>"
        + f"<p style='color: #8b949e; font-size: 13px;'>Handoff ID: <code>{hid}</code></p>"
        + _footer()
    )


def handoff_revoked_email(
    *,
    sender_email: str,
    session_title: str | None,
    reason: str,
    handoff_id: str,
) -> str:
    """Email sent to the recipient when the sender revokes a pending handoff."""
    title = _html.escape(session_title or "Untitled session")
    sender = _html.escape(sender_email)
    hid = _html.escape(handoff_id)
    return (
        _shell()
        + "<h2 style='margin: 0 0 8px 0; color: #e6edf3;'>Handoff revoked</h2>"
        + f"<p style='color: #8b949e;'>{sender} revoked the handoff <strong>{title}</strong>.</p>"
        + _quote_block(f"Reason from {sender_email}:", reason)
        + f"<p style='color: #8b949e; font-size: 13px;'>Handoff ID: <code>{hid}</code></p>"
        + _footer()
    )


def handoff_declined_email(
    *,
    recipient_email: str,
    session_title: str | None,
    reason: str | None,
    handoff_id: str,
) -> str:
    """Email sent to the sender when the recipient declines a handoff."""
    title = _html.escape(session_title or "Untitled session")
    recipient = _html.escape(recipient_email)
    hid = _html.escape(handoff_id)
    reason_html = (
        _quote_block(f"Reason from {recipient_email}:", reason)
        if reason
        else ""
    )
    return (
        _shell()
        + "<h2 style='margin: 0 0 8px 0; color: #e6edf3;'>Handoff declined</h2>"
        + f"<p style='color: #8b949e;'>{recipient} declined the handoff <strong>{title}</strong>.</p>"
        + reason_html
        + f"<p style='color: #8b949e; font-size: 13px;'>Handoff ID: <code>{hid}</code></p>"
        + _footer()
    )


def handoff_comment_email(
    *,
    author_email: str,
    session_title: str | None,
    content: str,
    handoff_id: str,
) -> str:
    """Email sent to the other party when someone comments on a handoff."""
    title = _html.escape(session_title or "Untitled session")
    author = _html.escape(author_email)
    hid = _html.escape(handoff_id)
    return (
        _shell()
        + "<h2 style='margin: 0 0 8px 0; color: #e6edf3;'>New comment on handoff</h2>"
        + f"<p style='color: #8b949e;'>{author} commented on the handoff <strong>{title}</strong>.</p>"
        + _quote_block(f"From {author_email}:", content)
        + f"<p style='color: #8b949e; font-size: 13px;'>Handoff ID: <code>{hid}</code></p>"
        + _footer()
    )


def org_invite_email(
    *,
    org_name: str,
    inviter_name: str,
    inviter_email: str,
    role: str,
    accept_url: str,
    expires_at_human: str,
) -> str:
    """Email sent to a user invited to join an organization.

    Lives in the v0.10.9 lifecycle-shell family; the accept_url is the
    dashboard's `/invites/<invite_id>` landing page (Atlas + Prism wired
    together in v0.10.22 — tk_6afbcfefe5804c1d). The recipient clicks,
    logs in if needed, then sees a one-click Accept CTA.
    """
    org = _html.escape(org_name)
    inviter_disp = _html.escape(inviter_name or inviter_email)
    role_disp = _html.escape(role)
    expires_disp = _html.escape(expires_at_human)
    # accept_url is server-built, not user-supplied — quoteattr-style
    # escape still applied for defense in depth.
    safe_url = _html.escape(accept_url, quote=True)
    return (
        _shell()
        + "<h2 style='margin: 0 0 8px 0; color: #e6edf3;'>"
        + f"You're invited to {org}</h2>"
        + "<p style='color: #8b949e;'>"
        + f"{inviter_disp} invited you to join the <strong>{org}</strong> "
        + f"organization on SessionFS as a <strong>{role_disp}</strong>.</p>"
        + "<p style='margin-top: 20px;'>"
        + f"<a href='{safe_url}' style='display: inline-block; background: #4f9cf7; "
        + "color: white; padding: 10px 24px; border-radius: 6px; text-decoration: none; "
        + "font-weight: 500;'>Accept invite</a></p>"
        + "<p style='color: #8b949e; font-size: 13px; margin-top: 16px;'>"
        + f"This invite expires {expires_disp}. If you don't recognize this "
        + "organization, you can safely ignore the email.</p>"
        + _footer()
    )


def _project_url(dashboard_url: str, project_id: str) -> str:
    """Filtered dashboard view for a project (Work section)."""
    base = dashboard_url.rstrip("/")
    return f"{base}/projects/{_html.escape(project_id, quote=True)}?tab=work"


def _opt_out_url(dashboard_url: str) -> str:
    base = dashboard_url.rstrip("/")
    return _html.escape(f"{base}/settings/organization?tab=policies", quote=True)


def org_digest_email(
    *,
    org_name: str,
    week_label: str,
    digest: dict,
    dashboard_url: str,
) -> str:
    """HTML body for the weekly org activity digest.

    Every user-controlled string (org name, project names, user display
    names/emails, persona names, audit event types) is ``html.escape``d.
    Links target the dashboard's filtered project views; the footer links
    to org settings where the recipient can opt out.
    """
    org = _html.escape(org_name)
    week = _html.escape(week_label)

    sessions = digest.get("sessions", {})
    tickets = digest.get("tickets", {})
    runs = digest.get("agent_runs", {})
    knowledge = digest.get("knowledge", {})
    handoffs = digest.get("handoffs", {})
    admin_actions = digest.get("admin_actions", {})

    top_projects_html = ""
    for p in sessions.get("top_projects", []):
        name = _html.escape(p.get("name") or p.get("project_id") or "Unknown")
        if dashboard_url:
            url = _project_url(dashboard_url, p["project_id"])
            name_cell = (
                f"<a href='{url}' style='color: #4f9cf7; text-decoration: none;'>{name}</a>"
            )
        else:
            name_cell = name
        top_projects_html += (
            f"<tr><td style='padding: 6px 12px; color: #e6edf3;'>{name_cell}</td>"
            f"<td style='padding: 6px 12px; color: #8b949e; text-align: right;'>{p['sessions']}</td></tr>"
        )

    top_users_html = ""
    for u in sessions.get("top_users", []):
        display = _html.escape(u.get("display_name") or u.get("email") or u.get("user_id") or "Unknown")
        top_users_html += (
            f"<tr><td style='padding: 6px 12px; color: #e6edf3;'>{display}</td>"
            f"<td style='padding: 6px 12px; color: #8b949e; text-align: right;'>{u['sessions']}</td></tr>"
        )
    former = sessions.get("former_members", 0)
    if former:
        top_users_html += (
            f"<tr><td style='padding: 6px 12px; color: #8b949e;'>Former members</td>"
            f"<td style='padding: 6px 12px; color: #8b949e; text-align: right;'>{former}</td></tr>"
        )

    def _kv_table(items: dict, label: str) -> str:
        if not items:
            return ""
        rows = "".join(
            f"<tr><td style='padding: 6px 12px; color: #e6edf3;'>{_html.escape(str(k))}</td>"
            f"<td style='padding: 6px 12px; color: #8b949e; text-align: right;'>{v}</td></tr>"
            for k, v in sorted(items.items(), key=lambda kv: (-kv[1], str(kv[0])))
        )
        return (
            f"<p style='color: #8b949e; margin: 12px 0 4px 0; font-size: 13px;'>{label}</p>"
            f"<table style='width: 100%; border-collapse: collapse; margin: 0 0 8px 0; "
            f"background: #161b22; border-radius: 6px; overflow: hidden;'>{rows}</table>"
        )

    opened_persona = _kv_table(tickets.get("opened_by_persona", {}), "Opened by persona")
    completed_persona = _kv_table(tickets.get("completed_by_persona", {}), "Completed by persona")
    run_status = _kv_table(runs.get("by_status", {}), "By status")
    severity = _kv_table(runs.get("severity_rollup", {}), "Findings by severity")
    audit = _kv_table(admin_actions.get("by_action", {}), "By action")

    return (
        _shell()
        + "<h2 style='margin: 0 0 4px 0; color: #e6edf3;'>"
        + f"{org} weekly activity</h2>"
        + f"<p style='color: #8b949e; margin: 0 0 24px 0;'>Week of {week}</p>"
        # Sessions
        + "<h3 style='color: #e6edf3; margin: 16px 0 8px 0;'>Sessions</h3>"
        + f"<p style='color: #e6edf3; margin: 0 0 8px 0;'>{sessions.get('total', 0)} sessions</p>"
        + ((
            "<p style='color: #8b949e; margin: 12px 0 4px 0; font-size: 13px;'>Top projects</p>"
            f"<table style='width: 100%; border-collapse: collapse; margin: 0 0 8px 0; "
            f"background: #161b22; border-radius: 6px; overflow: hidden;'>{top_projects_html}</table>"
        ) if top_projects_html else "")
        + ((
            "<p style='color: #8b949e; margin: 12px 0 4px 0; font-size: 13px;'>Top users</p>"
            f"<table style='width: 100%; border-collapse: collapse; margin: 0 0 8px 0; "
            f"background: #161b22; border-radius: 6px; overflow: hidden;'>{top_users_html}</table>"
        ) if top_users_html else "")
        # Tickets
        + "<h3 style='color: #e6edf3; margin: 16px 0 8px 0;'>Tickets</h3>"
        + "<p style='color: #e6edf3; margin: 0;'>"
        + f"{tickets.get('opened', 0)} opened · {tickets.get('completed', 0)} completed</p>"
        + opened_persona
        + completed_persona
        # Agent runs
        + "<h3 style='color: #e6edf3; margin: 16px 0 8px 0;'>Agent runs</h3>"
        + f"<p style='color: #e6edf3; margin: 0;'>{runs.get('total', 0)} runs</p>"
        + run_status
        + severity
        # Knowledge
        + "<h3 style='color: #e6edf3; margin: 16px 0 8px 0;'>Knowledge base</h3>"
        + "<p style='color: #e6edf3; margin: 0;'>"
        + f"{knowledge.get('claims_added', 0)} claims added · "
        + f"{knowledge.get('claims_promoted', 0)} promoted · "
        + f"{knowledge.get('claims_dismissed', 0)} dismissed</p>"
        # Handoffs
        + "<h3 style='color: #e6edf3; margin: 16px 0 8px 0;'>Handoffs</h3>"
        + "<p style='color: #e6edf3; margin: 0;'>"
        + f"{handoffs.get('created', 0)} created · {handoffs.get('claimed', 0)} claimed</p>"
        # Admin actions
        + "<h3 style='color: #e6edf3; margin: 16px 0 8px 0;'>Admin activity</h3>"
        + f"<p style='color: #e6edf3; margin: 0;'>{admin_actions.get('total', 0)} actions</p>"
        + audit
        # Footer with opt-out (skipped when there is no dashboard URL to link to)
        + (
            "<p style='color: #484f58; font-size: 12px; margin-top: 24px; border-top: 1px solid #21262d; "
            "padding-top: 16px;'>"
            f"This weekly digest is sent to {org} owners and admins. "
            + (
                f"<a href='{_opt_out_url(dashboard_url)}' style='color: #4f9cf7;'>"
                "Manage email preferences</a>."
                if dashboard_url
                else ""
            )
            + "</p>"
            "</div>"
        )
    )


def org_digest_text(
    *,
    org_name: str,
    week_label: str,
    digest: dict,
    dashboard_url: str,
) -> str:
    """Plain-text alternative for the weekly org activity digest."""
    lines: list[str] = [
        f"SessionFS — {org_name} weekly activity",
        f"Week of {week_label}",
        "",
    ]
    sessions = digest.get("sessions", {})
    lines.append(f"Sessions: {sessions.get('total', 0)}")
    for p in sessions.get("top_projects", []):
        lines.append(f"  - {p.get('name') or p.get('project_id')}: {p['sessions']}")
    for u in sessions.get("top_users", []):
        lines.append(f"  - {u.get('display_name') or u.get('email') or u.get('user_id')}: {u['sessions']}")
    if sessions.get("former_members"):
        lines.append(f"  - Former members: {sessions['former_members']}")

    tickets = digest.get("tickets", {})
    lines.append("")
    lines.append(f"Tickets: {tickets.get('opened', 0)} opened · {tickets.get('completed', 0)} completed")
    for k, v in sorted(tickets.get("opened_by_persona", {}).items(), key=lambda kv: (-kv[1], str(kv[0]))):
        lines.append(f"  opened by {k}: {v}")
    for k, v in sorted(tickets.get("completed_by_persona", {}).items(), key=lambda kv: (-kv[1], str(kv[0]))):
        lines.append(f"  completed by {k}: {v}")

    runs = digest.get("agent_runs", {})
    lines.append("")
    lines.append(f"Agent runs: {runs.get('total', 0)}")
    for k, v in sorted(runs.get("by_status", {}).items()):
        lines.append(f"  {k}: {v}")
    for k, v in sorted(runs.get("severity_rollup", {}).items()):
        lines.append(f"  findings ({k}): {v}")

    knowledge = digest.get("knowledge", {})
    lines.append("")
    lines.append(
        f"Knowledge base: {knowledge.get('claims_added', 0)} added · "
        f"{knowledge.get('claims_promoted', 0)} promoted · {knowledge.get('claims_dismissed', 0)} dismissed"
    )

    handoffs = digest.get("handoffs", {})
    lines.append("")
    lines.append(f"Handoffs: {handoffs.get('created', 0)} created · {handoffs.get('claimed', 0)} claimed")

    admin_actions = digest.get("admin_actions", {})
    lines.append("")
    lines.append(f"Admin activity: {admin_actions.get('total', 0)} actions")
    for k, v in sorted(admin_actions.get("by_action", {}).items()):
        lines.append(f"  {k}: {v}")

    lines.append("")
    if dashboard_url:
        lines.append(
            f"Manage email preferences: {dashboard_url.rstrip('/')}/settings/organization?tab=policies"
        )
    return "\n".join(lines)


def transfer_proposed_email(
    *,
    project_name: str,
    org_name: str,
    proposer_name: str,
    reason: str,
    expires_at_human: str,
) -> str:
    """Email sent to a project owner when an org admin proposes a transfer."""
    project = _html.escape(project_name or "Untitled project")
    org = _html.escape(org_name)
    proposer = _html.escape(proposer_name)
    expires = _html.escape(expires_at_human)
    return (
        _shell()
        + "<h2 style='margin: 0 0 8px 0; color: #e6edf3;'>Project transfer proposed</h2>"
        + f"<p style='color: #8b949e;'>{proposer} proposed moving "
        + f"<strong>{project}</strong> into the <strong>{org}</strong> "
        + "organization on SessionFS.</p>"
        + _quote_block(f"Reason from {proposer_name}:", reason)
        + "<p style='color: #8b949e; font-size: 13px; margin-top: 16px;'>"
        + "Open the Transfers inbox to accept or decline this proposal. "
        + f"The proposal expires {expires} if you don't respond.</p>"
        + _footer()
    )


def transfer_proposal_declined_email(
    *,
    project_name: str,
    org_name: str,
    owner_name: str,
) -> str:
    """Email sent to the proposer when the project owner declines a proposal."""
    project = _html.escape(project_name or "Untitled project")
    org = _html.escape(org_name)
    owner = _html.escape(owner_name)
    return (
        _shell()
        + "<h2 style='margin: 0 0 8px 0; color: #e6edf3;'>Transfer proposal declined</h2>"
        + f"<p style='color: #8b949e;'>{owner} declined your proposal to move "
        + f"<strong>{project}</strong> into the <strong>{org}</strong> "
        + "organization.</p>"
        + _footer()
    )


def transfer_proposal_expired_email(
    *,
    project_name: str,
    org_name: str,
    owner_name: str,
) -> str:
    """Email sent to the proposer when a proposal lapses without a response."""
    project = _html.escape(project_name or "Untitled project")
    org = _html.escape(org_name)
    owner = _html.escape(owner_name)
    return (
        _shell()
        + "<h2 style='margin: 0 0 8px 0; color: #e6edf3;'>Transfer proposal expired</h2>"
        + f"<p style='color: #8b949e;'>Your proposal to move <strong>{project}</strong> "
        + f"into the <strong>{org}</strong> organization expired without a "
        + f"response from {owner}.</p>"
        + "<p style='color: #8b949e; font-size: 13px;'>You can propose the transfer "
        + "again if it is still needed.</p>"
        + _footer()
    )


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
    landing_url: str | None = None,
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

    # P1 pre-signup landing: a public page the recipient can view BEFORE
    # creating an account. The token lives in the URL fragment — browser-
    # side only, never logged by servers. Signup becomes the CLAIM action.
    landing_html = ""
    if landing_url:
        safe_landing = _html.escape(landing_url, quote=True)
        landing_html = (
            "<div style='background: #161b22; padding: 16px; border-radius: 6px; "
            "margin: 16px 0; text-align: center;'>"
            "<p style='color: #e6edf3; margin: 0 0 4px 0; font-weight: 500;'>"
            "You've received a session handoff</p>"
            "<p style='color: #8b949e; margin: 0 0 12px 0; font-size: 13px;'>"
            "View the session preview — no account needed.</p>"
            f"<a href='{safe_landing}' style='display: inline-block; "
            "background: #4f9cf7; color: white; padding: 10px 24px; "
            "border-radius: 6px; text-decoration: none; font-weight: 500;'>"
            "View handoff →</a></div>"
        )

    dashboard_html = ""
    if dashboard_url and not landing_url:
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
        # P1 landing-page CTA — view the session preview without signing up
        f"{landing_html}"
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


def ticket_notification_email(
    *,
    notifications: list[dict],
    dashboard_url: str,
) -> str:
    """One email digest of watched-ticket notifications for a user.

    Each item is ``{"ticket_title", "ticket_id", "project_id", "kind",
    "summary"}``. ``summary`` is server-generated (e.g. "Assigned to you")
    and never copies a comment body, so it can be rendered inline safely.
    """
    from sessionfs.server.services.ticket_watchers import KIND_LABELS

    count = len(notifications)
    items_html = []
    for item in notifications:
        label = KIND_LABELS.get(
            item["kind"], str(item["kind"]).replace("_", " ").title()
        )
        url = (
            dashboard_url.rstrip("/")
            + "/projects/"
            + _html.escape(str(item["project_id"]), quote=True)
            + "?tab=work&sub=tickets&ticket="
            + _html.escape(str(item["ticket_id"]), quote=True)
        )
        items_html.append(
            "<div style='background: #161b22; padding: 12px 16px; margin: 8px 0; "
            "border-radius: 6px;'>"
            f"<p style='margin: 0 0 4px 0; font-size: 12px; color: #4f9cf7;'>{_html.escape(label)}</p>"
            f"<p style='margin: 0 0 4px 0; font-weight: 500;'>{_html.escape(item['ticket_title'] or 'Untitled ticket')}</p>"
            f"<p style='margin: 0 0 8px 0; color: #8b949e;'>{_html.escape(item['summary'])}</p>"
            f"<a href='{url}' style='color: #4f9cf7; font-size: 13px;'>View ticket</a>"
            "</div>"
        )
    plural = "" if count == 1 else "s"
    return (
        _shell()
        + "<div style='margin-bottom: 24px;'>"
        + f"<h2 style='margin: 0 0 8px 0; color: #e6edf3;'>{count} ticket update{plural}</h2>"
        + "<p style='margin: 0; color: #8b949e;'>Tickets you follow changed.</p></div>"
        + "".join(items_html)
        + _footer()
    )


def ticket_notification_text(
    *,
    notifications: list[dict],
    dashboard_url: str,
) -> str:
    """Plain-text fallback for the ticket-notification email."""
    lines = [f"{len(notifications)} ticket update(s)\n"]
    for item in notifications:
        lines.append(
            f"{item['ticket_title'] or 'Untitled ticket'}\n{item['summary']}\n"
            f"{dashboard_url.rstrip('/')}/projects/{item['project_id']}"
            f"?tab=work&sub=tickets&ticket={item['ticket_id']}\n"
        )
    return "\n".join(lines)
