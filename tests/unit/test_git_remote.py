"""normalize_git_remote maps every URL form of a repository to one value (GitHub #88)."""

from __future__ import annotations

import pytest

from sessionfs.git_remote import normalize_git_remote


@pytest.mark.parametrize(
    "remote",
    [
        "group/project",
        "Group/Project",
        "group/project.git",
        "group/project/",
        "https://gitlab.example.com/group/project",
        "https://gitlab.example.com/group/project.git",
        "https://gitlab.example.com/group/project/",
        "https://gitlab.example.com/group/project.git/",
        "HTTPS://GitLab.example.com/Group/Project",
        "http://gitlab.example.com:8080/group/project",
        "https://oauth2:token@gitlab.example.com/group/project.git",
        "git@gitlab.example.com:group/project.git",
        "git@gitlab.example.com:group/project.git/",
        "deploy@gitlab.example.com:group/project.git",
        "gitlab.example.com:group/project",
        "ssh://git@gitlab.example.com/group/project.git",
        "ssh://git@gitlab.example.com:2222/group/project.git",
        "git://gitlab.example.com/group/project.git",
        "git+ssh://git@gitlab.example.com/group/project.git",
        "  https://gitlab.example.com/group/project  ",
    ],
)
def test_url_forms_of_one_repo_agree(remote: str) -> None:
    assert normalize_git_remote(remote) == "group/project"


def test_subgroups_and_ipv6_hosts() -> None:
    assert normalize_git_remote("https://gitlab.example.com:8443/g/sub/repo.git") == "g/sub/repo"
    assert normalize_git_remote("https://[::1]:8080/a/b.git") == "a/b"


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("", ""),
        ("git@example.com:/srv/git/project.git", "/srv/git/project"),
        ("git@host:~user/repo.git", "~user/repo"),
        ("/srv/git/project.git", "/srv/git/project"),
        ("file:///srv/repos/foo.git", "file:///srv/repos/foo"),
        ("c:/repo", "c:/repo"),  # a drive letter is not an scp host
        ("https://host", "https://host"),  # no repository path
    ],
)
def test_paths_and_edge_cases(remote: str, expected: str) -> None:
    assert normalize_git_remote(remote) == expected


def test_personal_server_paths_keep_their_host() -> None:
    """A home-dir or absolute path means nothing without its host, so two
    people's ~/app on different machines must not become one project."""
    assert normalize_git_remote("alice@hostA:~/app") == "alice@hosta:~/app"
    assert normalize_git_remote("bob@hostB:~/app.git") == "bob@hostb:~/app"
    assert normalize_git_remote("srv1:/srv/git/app") == "srv1:/srv/git/app"
    assert normalize_git_remote("ssh://box/~/app") == "ssh://box/~/app"
    # git@ remotes dropped the host before v0.22 and still do; so does https,
    # where ~user is a forge namespace (sourcehut).
    assert normalize_git_remote("git@host:~user/repo.git") == "~user/repo"
    assert normalize_git_remote("ssh://git@git.sr.ht/~user/repo") == "~user/repo"
    assert normalize_git_remote("https://git.sr.ht/~user/repo") == "~user/repo"


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:Org/Repo.git", "ssh://git@h:22/g/r", "alice@hostA:~/app",
        "srv1:/srv/git/app", "ssh://box/~/app", "deploy@h.example:g/sub/r/",
        "/srv/git/project", "~user/repo", "file:///srv/x.git", "group/project",
    ],
)
def test_normalizing_twice_changes_nothing(remote: str) -> None:
    once = normalize_git_remote(remote)
    assert normalize_git_remote(once) == once
