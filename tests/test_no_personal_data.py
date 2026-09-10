"""Guard against committing real identities, personal paths, or credentials.

`AGENTS.md` already states the rule -- never hardcode a real absolute path, a personal home
directory, a specific hostname -- but prose is not enforcement. This file makes the rule executable,
because the failure it prevents is one that cannot be undone by a follow-up commit: anything pushed
to a public repository is public from that moment, and removing it afterwards means rewriting
history, which breaks every clone and, where pull-request refs pin the old commits, means deleting
and recreating the repository outright.

Deliberately NOT a generic secret scanner. Tools that key on token shapes and entropy catch API
keys, and they would not have caught the identifiers that actually leaked here: a machine name and
an OS account name are ordinary-looking words with no distinguishing entropy. So the checks below
come in two halves -- a small credential net for the obvious cases, and a project-specific identity
net for the ones no off-the-shelf tool can know about.

Placeholders must stay obviously fake. That is what keeps the identity check cheap: rather than
recognising which names are real, it recognises the short list that is allowed and refuses the rest.
"""

import hashlib
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Names that may legitimately appear in an example path. Anything else in that position is treated
# as somebody's real account until a maintainer says otherwise by adding it here.
PLACEHOLDER_NAMES = frozenset(
    {
        "you", "user", "username", "<username>", "someone", "jsmith", "jane", "johndoe",
        "researcher", "a-researcher", "example", "test", "me", "your-user", "youruser",
        "someoneelse",   # used as a deliberate stand-in for another machine's user in test_image_ocr
        "runner",        # GitHub Actions' home directory on Linux runners
        "runneradmin",   # ... and on Windows runners
    }
)

# Domains an email address may use in committed text. Real correspondence addresses do not belong in
# a public repository, and GitHub's noreply form exists precisely so commits need not carry one.
ALLOWED_EMAIL_DOMAINS = (
    "example.com",
    "example.org",
    "users.noreply.github.com",
    "noreply.github.com",
)

CREDENTIAL_PATTERNS = {
    "GitHub personal access token": re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    "GitHub fine-grained token": re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    "OpenAI-style API key": re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----"),
}

# A Windows or POSIX home directory naming somebody. The captured group is the account name, which
# is checked against PLACEHOLDER_NAMES rather than rejected outright -- documented example paths are
# useful and should stay legal.
HOME_DIRECTORY_PATTERNS = (
    re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+([A-Za-z0-9._-]+)"),
    re.compile(r"/home/([A-Za-z0-9._-]+)"),
    re.compile(r"/Users/([A-Za-z0-9._-]+)"),
)

EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

# Institution-specific identifiers. This is the half a generic scanner cannot supply: only a
# maintainer knows that an asset tag of this shape names a real machine.
INSTITUTION_PATTERNS = {
    "institutional asset-tag hostname": re.compile(r"\bLIF-\d{4,}\b"),
}

# Forbidden whole words, stored as digests rather than literals.
#
# A regex that forbids a word has to contain that word, which made this module the one place in the
# repository allowed to hold exactly what the repository must not -- and it did: an earlier revision
# carried the real machine name in a test sample, invisible because the module exempted itself from
# its own scan. Digests remove the need for that exemption. The guard still matches the words in CI;
# the words are no longer here to leak, and no future edit can reintroduce one without its digest
# ceasing to match.
#
# Whole words only, which is why the asset-tag pattern above stays a regex.
#
# To add a word:  python -c "import hashlib;print(hashlib.sha256(b'theword').hexdigest())"
FORBIDDEN_WORD_DIGESTS = {
    "institution name": "899f6c267119bf3a4ec940ca1caafcfacdefe40060b6e80f6613472fc1064a9b",
    "institution city": "021279569379206221415512c26bcad47c3b3e4ca3e5684b146a4b5368ae9079",
}

WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")


def _forbidden_words_in(text: str, digests: dict[str, str]) -> set[str]:
    """Labels of any forbidden word appearing in ``text``, matched case-insensitively."""
    wanted = {digest: label for label, digest in digests.items()}
    found = set()
    for word in WORD_PATTERN.findall(text):
        label = wanted.get(hashlib.sha256(word.lower().encode("utf-8")).hexdigest())
        if label is not None:
            found.add(label)
    return found

# Nothing is exempt. Every forbidden literal in this module is either built by concatenation at
# runtime or stored as a digest, so the module passes its own scan. That is deliberate: the former
# exemption is exactly how the real machine name came to sit here unnoticed.
EXEMPT: frozenset[str] = frozenset()

TEXT_SUFFIXES = frozenset(
    {
        ".py", ".md", ".toml", ".txt", ".json", ".yml", ".yaml", ".cfg", ".ini",
        ".ps1", ".sh", ".bat", ".html", ".css", ".js", ".jsonl", ".csv", ".lock", "",
    }
)


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, check=True
    )


def _tracked_text_files(repo_root: Path = REPO_ROOT) -> list[str]:
    """Every git-tracked text file, as repo-relative POSIX paths."""
    result = _git(repo_root, "ls-files", "-z")
    return [
        line
        for line in result.stdout.split("\0")
        if line and Path(line).suffix.lower() in TEXT_SUFFIXES and line not in EXEMPT
    ]


def _paths_staged_differently(repo_root: Path = REPO_ROOT) -> list[str]:
    """Tracked paths whose staged blob differs from the file on disk.

    A commit publishes the index, not the working tree, and the two can disagree: stage a file,
    edit it again without re-staging, and the version on disk is not the version about to become
    public. Everywhere they agree the working tree already speaks for the blob, so only the
    disagreements need reading out of the index -- usually none, and never many.
    """
    result = _git(repo_root, "diff", "--name-only", "-z")
    return [path for path in result.stdout.split("\0") if path]


def _read_worktree(repo_root: Path, relative_path: str) -> str | None:
    try:
        return (repo_root / relative_path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # Binary or unreadable: nothing a text pattern could match anyway.


def _read_staged(repo_root: Path, relative_path: str) -> str | None:
    """The stage-0 blob for a path, or None if there isn't one to read."""
    result = subprocess.run(
        ["git", "show", f":{relative_path}"], cwd=repo_root, capture_output=True, check=False
    )
    if result.returncode != 0:
        return None  # Staged as deleted, or an unmerged path with no stage 0.
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def collect_scannable_contents(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """Every text a commit from this repository could publish, keyed by a human-readable label.

    Scans both the working tree and, where it differs, the index. Scanning only one of them leaves
    a hole: the working tree alone lets a partial stage publish a string that was edited away
    afterwards, and the index alone stops reporting a forbidden string the moment you type it,
    which is the point at which it is cheapest to fix. Labels for staged blobs are suffixed so a
    failure says which of the two it came from.
    """
    contents: dict[str, str] = {}
    text_files = _tracked_text_files(repo_root)
    for path in text_files:
        text = _read_worktree(repo_root, path)
        if text is not None:
            contents[path] = text

    scannable = set(text_files)
    for path in _paths_staged_differently(repo_root):
        if path not in scannable:
            continue
        text = _read_staged(repo_root, path)
        if text is not None:
            contents[f"{path} (staged)"] = text
    return contents


class NoPersonalDataTests(unittest.TestCase):
    """The repository must not carry anyone's real identity, machine, or credentials."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contents = collect_scannable_contents()
        if not cls.contents:
            raise AssertionError("No tracked text files found -- the guard would pass vacuously.")

    def test_no_credentials_are_committed(self):
        """The obvious half: token shapes a generic scanner would also catch."""
        for path, text in self.contents.items():
            for label, pattern in CREDENTIAL_PATTERNS.items():
                with self.subTest(check=label, file=path):
                    match = pattern.search(text)
                    self.assertIsNone(
                        match,
                        f"{path} appears to contain a {label}. Remove it, rotate the credential, "
                        "and do not commit the replacement.",
                    )

    def test_no_real_home_directories(self):
        """Example paths are fine; somebody's actual account name inside one is not."""
        for path, text in self.contents.items():
            for pattern in HOME_DIRECTORY_PATTERNS:
                for match in pattern.finditer(text):
                    name = match.group(1)
                    with self.subTest(file=path, account=name):
                        self.assertIn(
                            name.lower(),
                            PLACEHOLDER_NAMES,
                            f"{path} contains the home directory '{match.group(0)}', which names a "
                            f"real account. Use a placeholder such as 'you' or 'jsmith', or add "
                            f"'{name}' to PLACEHOLDER_NAMES if it genuinely is one.",
                        )

    def test_no_real_email_addresses(self):
        for path, text in self.contents.items():
            for match in EMAIL_PATTERN.finditer(text):
                domain = match.group(1).lower()
                with self.subTest(file=path, domain=domain):
                    self.assertTrue(
                        domain.endswith(ALLOWED_EMAIL_DOMAINS),
                        f"{path} contains the address '{match.group(0)}'. Use an example.com "
                        "address in documentation, or the GitHub noreply form for authorship.",
                    )

    def test_no_institutional_identifiers(self):
        """The half no off-the-shelf scanner can supply.

        A machine name shaped like an asset tag is an ordinary-looking string with no entropy
        signature, so nothing generic flags it. It is also exactly what leaked here before, which is
        why the list is explicit rather than heuristic.
        """
        for path, text in self.contents.items():
            for label, pattern in INSTITUTION_PATTERNS.items():
                with self.subTest(check=label, file=path):
                    match = pattern.search(text)
                    self.assertIsNone(
                        match,
                        f"{path} contains what looks like an {label}"
                        + (f" ({match.group(0)})" if match else "")
                        + ". Replace it with a placeholder.",
                    )
            with self.subTest(check="forbidden words", file=path):
                found = _forbidden_words_in(text, FORBIDDEN_WORD_DIGESTS)
                self.assertEqual(
                    found,
                    set(),
                    f"{path} names something this project does not publish ({', '.join(sorted(found))}). "
                    "The word is stored as a digest, so this message cannot quote it back to you.",
                )

    def test_the_guard_actually_matches_the_things_it_forbids(self):
        """Without this, a broken regex would make every check above pass silently.

        A guard that scans real files and finds nothing looks identical to a guard that scans real
        files and *cannot* find anything. These assertions distinguish the two.
        """
        cases = [
            (CREDENTIAL_PATTERNS["GitHub personal access token"], "ghp_" + "A" * 36),
            (CREDENTIAL_PATTERNS["AWS access key id"], "AKIA" + "B" * 16),
            (INSTITUTION_PATTERNS["institutional asset-tag hostname"], "host LIF-" + "9" * 6),
            (HOME_DIRECTORY_PATTERNS[0], "C:" + chr(92) + "Users" + chr(92) + "someone"),
            (HOME_DIRECTORY_PATTERNS[1], "/home/someone/.config"),
            (EMAIL_PATTERN, "write to person@" + "university.example"),
        ]
        for pattern, sample in cases:
            with self.subTest(sample=sample):
                self.assertIsNotNone(pattern.search(sample), f"pattern failed to match {sample!r}")

        # The digest matcher gets a sentinel of its own: the real words cannot appear here, so
        # without this the whole word check could silently match nothing.
        sentinel = "guardsentinel" + "word"
        sentinel_digests = {
            "sentinel": "e7860b7e5ec3fcfd4754182a761f4cb117e9bf5b4c3bef0492400bfb4832a0a2"
        }
        self.assertEqual(
            _forbidden_words_in(f"a {sentinel.upper()} in prose", sentinel_digests), {"sentinel"}
        )
        self.assertEqual(_forbidden_words_in("nothing to see", sentinel_digests), set())

        # And the allowlist must actually allow: a documented placeholder path must not trip it.
        match = HOME_DIRECTORY_PATTERNS[0].search("C:" + chr(92) + "Users" + chr(92) + "you")
        self.assertIsNotNone(match)
        self.assertIn(match.group(1).lower(), PLACEHOLDER_NAMES)


class StagedContentIsScannedTests(unittest.TestCase):
    """What a commit publishes is the index, so that is what the guard has to be able to see."""

    def test_a_forbidden_string_hidden_by_a_later_edit_is_still_found(self):
        """Stage a forbidden path, then edit it away without re-staging.

        Reading only the working tree makes this look clean while the commit about to be made
        still carries the account name. The assertions below check the premise -- that the file on
        disk really is clean -- before checking the conclusion, so the test cannot pass for the
        wrong reason.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for args in (
                ("init", "-q"),
                ("config", "user.email", "test@example.com"),
                ("config", "user.name", "Test"),
            ):
                _git(root, *args)

            document = root / "example.md"
            document.write_text("see /home/" + "private_account_demo" + "/paper.pdf\n", encoding="utf-8")
            _git(root, "add", "example.md")
            document.write_text("see /home/you/paper.pdf\n", encoding="utf-8")

            contents = collect_scannable_contents(root)

            self.assertIn("example.md", contents)
            self.assertNotIn(
                "private_account_demo",
                contents["example.md"],
                "premise: the working tree must look clean, or the test proves nothing",
            )
            self.assertIn(
                "example.md (staged)",
                contents,
                "the staged blob differs from disk and is what a commit would publish",
            )
            self.assertIn("private_account_demo", contents["example.md (staged)"])

            # And the account name must be one the home-directory check actually rejects.
            match = HOME_DIRECTORY_PATTERNS[1].search(contents["example.md (staged)"])
            self.assertIsNotNone(match)
            self.assertNotIn(match.group(1).lower(), PLACEHOLDER_NAMES)

    def test_an_unstaged_edit_is_still_reported(self):
        """The index must not become the only thing scanned.

        A forbidden string is cheapest to fix at the moment it is typed, which is before it is
        staged. Adding index coverage must not cost that.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for args in (
                ("init", "-q"),
                ("config", "user.email", "test@example.com"),
                ("config", "user.name", "Test"),
            ):
                _git(root, *args)

            document = root / "example.md"
            document.write_text("clean\n", encoding="utf-8")
            _git(root, "add", "example.md")
            _git(root, "commit", "-qm", "seed")
            document.write_text("see /home/" + "private_account_demo" + "/paper.pdf\n", encoding="utf-8")

            contents = collect_scannable_contents(root)

            self.assertIn("private_account_demo", contents["example.md"])


if __name__ == "__main__":
    unittest.main()
