"""Skills loader for agent capabilities."""

import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

import yaml

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"

# Opening ---, YAML body (group 1), closing --- on its own line; supports CRLF.
_STRIP_SKILL_FRONTMATTER = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?",
    re.DOTALL,
)

# Characters allowed in a skill directory name. Keeps traversal-safe names.
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_valid_skill_name(name: str) -> bool:
    """Return True if *name* is a safe skill directory name."""
    return bool(name) and _SKILL_NAME_RE.match(name) is not None


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None, disabled_skills: set[str] | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        # Initial disabled set; refreshed from live config on each list_skills()
        # so toggling a skill at runtime takes effect on the next turn.
        self._initial_disabled_skills = set(disabled_skills) if disabled_skills else set()
        self.disabled_skills = set(self._initial_disabled_skills)

    def _refresh_disabled_from_config(self) -> None:
        """Refresh ``self.disabled_skills`` from the live config file.

        Merges the constructor-provided set with config's ``disabled_skills``
        so runtime toggles (saved to config) take effect without restart.
        """
        try:
            from miniunicorn.config.loader import load_config

            config = load_config()
            config_disabled = set(getattr(config.agents.defaults, "disabled_skills", []) or [])
        except Exception:
            config_disabled = set()
        self.disabled_skills = self._initial_disabled_skills | config_disabled

    def set_disabled_skills(self, names: set[str] | None) -> None:
        """Override the constructor-provided disabled set (used for hot reload)."""
        self._initial_disabled_skills = set(names) if names else set()
        self.disabled_skills = set(self._initial_disabled_skills)

    def _skill_entries_from_dir(self, base: Path, source: str, *, skip_names: set[str] | None = None) -> list[dict[str, str]]:
        if not base.exists():
            return []
        entries: list[dict[str, str]] = []
        for skill_dir in base.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            name = skill_dir.name
            if skip_names is not None and name in skip_names:
                continue
            entries.append({"name": name, "path": str(skill_file), "source": source})
        return entries

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        self._refresh_disabled_from_config()
        skills = self._skill_entries_from_dir(self.workspace_skills, "workspace")
        workspace_names = {entry["name"] for entry in skills}
        if self.builtin_skills and self.builtin_skills.exists():
            skills.extend(
                self._skill_entries_from_dir(self.builtin_skills, "builtin", skip_names=workspace_names)
            )

        if self.disabled_skills:
            skills = [s for s in skills if s["name"] not in self.disabled_skills]

        if filter_unavailable:
            return [skill for skill in skills if self._check_requirements(self._get_skill_meta(skill["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        roots = [self.workspace_skills]
        if self.builtin_skills:
            roots.append(self.builtin_skills)
        for root in roots:
            path = root / name / "SKILL.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = [
            f"### Skill: {name}\n\n{self._strip_frontmatter(markdown)}"
            for name in skill_names
            if (markdown := self.load_skill(name))
        ]
        return "\n\n---\n\n".join(parts)

    def build_skills_summary(self, exclude: set[str] | None = None) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        Args:
            exclude: Set of skill names to omit from the summary.

        Returns:
            Markdown-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        lines: list[str] = []
        for entry in all_skills:
            skill_name = entry["name"]
            if exclude and skill_name in exclude:
                continue
            meta = self._get_skill_meta(skill_name)
            available = self._check_requirements(meta)
            desc = self._get_skill_description(skill_name)
            if available:
                lines.append(f"- **{skill_name}** — {desc}  `{entry['path']}`")
            else:
                missing = self._get_missing_requirements(meta)
                suffix = f" (unavailable: {missing})" if missing else " (unavailable)"
                lines.append(f"- **{skill_name}** — {desc}{suffix}  `{entry['path']}`")
        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return ", ".join(
            [f"CLI: {command_name}" for command_name in required_bins if not shutil.which(command_name)]
            + [f"ENV: {env_name}" for env_name in required_env_vars if not os.environ.get(env_name)]
        )

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if not content.startswith("---"):
            return content
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if match:
            return content[match.end():].strip()
        return content

    def _parse_miniunicorn_metadata(self, raw: object) -> dict:  # noqa: N802
        """Extract MiniUnicorn/openclaw metadata from a frontmatter field.

        ``raw`` may be a dict (already parsed by yaml.safe_load) or a JSON str.
        """
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        else:
            return {}
        if not isinstance(data, dict):
            return {}
        payload = data.get("miniunicorn", data.get("MiniUnicorn", data.get("openclaw", {})))
        return payload if isinstance(payload, dict) else {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return all(shutil.which(cmd) for cmd in required_bins) and all(
            os.environ.get(var) for var in required_env_vars
        )

    def _get_skill_meta(self, name: str) -> dict:
        """Get MiniUnicorn metadata for a skill (cached in frontmatter)."""
        raw_meta = self.get_skill_metadata(name) or {}
        return self._parse_miniunicorn_metadata(raw_meta.get("metadata"))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        return [
            entry["name"]
            for entry in self.list_skills(filter_unavailable=True)
            if (meta := self.get_skill_metadata(entry["name"]) or {})
            and (
                self._parse_miniunicorn_metadata(meta.get("metadata")).get("always")
                or meta.get("always")
            )
        ]

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content or not content.startswith("---"):
            return None
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if not match:
            return None
        try:
            parsed = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None
        if not isinstance(parsed, dict):
            return None
        # yaml.safe_load returns native types (int, bool, list, etc.);
        # keep values as-is so downstream consumers get correct types.
        metadata: dict[str, object] = {}
        for key, value in parsed.items():
            metadata[str(key)] = value
        return metadata

    # -- Workspace skill management (create / edit / upload) ---------------

    def get_skill_dir(self, name: str) -> Path | None:
        """Return the directory holding a skill, or None if not found."""
        if not is_valid_skill_name(name):
            return None
        roots = [self.workspace_skills]
        if self.builtin_skills:
            roots.append(self.builtin_skills)
        for root in roots:
            path = root / name
            if (path / "SKILL.md").exists():
                return path
        return None

    def is_builtin_skill(self, name: str) -> bool:
        """Return True if a skill exists only in the builtin directory."""
        if not is_valid_skill_name(name):
            return False
        ws_path = self.workspace_skills / name / "SKILL.md"
        builtin_path = self.builtin_skills / name / "SKILL.md" if self.builtin_skills else None
        if builtin_path and builtin_path.exists():
            return not ws_path.exists()
        return False

    def save_skill_content(self, name: str, content: str) -> Path:
        """Create or overwrite a workspace skill's SKILL.md.

        Returns the path to the written file. Raises ValueError for invalid
        names or empty content.
        """
        if not is_valid_skill_name(name):
            raise ValueError(f"invalid skill name: {name!r}")
        if not content.strip():
            raise ValueError("skill content must not be empty")
        skill_dir = self.workspace_skills / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(content, encoding="utf-8")
        return skill_file

    def list_skill_files(self, name: str) -> list[str]:
        """Return relative paths of all files bundled with a skill."""
        skill_dir = self.get_skill_dir(name)
        if skill_dir is None or not skill_dir.exists():
            return []
        files: list[str] = []
        for path in sorted(skill_dir.rglob("*")):
            if path.is_file():
                files.append(str(path.relative_to(skill_dir)))
        return files

    def read_skill_file(self, name: str, rel_path: str) -> str | None:
        """Read a bundled skill file by relative path (traversal-safe)."""
        skill_dir = self.get_skill_dir(name)
        if skill_dir is None:
            return None
        # Resolve and ensure the target stays inside the skill dir.
        target = (skill_dir / rel_path).resolve()
        try:
            target.relative_to(skill_dir.resolve())
        except ValueError:
            return None
        if not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return None

    def extract_zip_skill(self, data: bytes, preferred_name: str | None = None) -> str:
        """Extract a ZIP skill package into the workspace skills directory.

        The ZIP must contain either a single top-level directory with a
        SKILL.md, or SKILL.md at the root. Returns the resulting skill name.
        Raises ValueError for malformed or unsafe archives.
        """
        if not data:
            raise ValueError("empty zip data")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ValueError("invalid zip file") from exc

        names = [n for n in zf.namelist() if n and not n.endswith("/")]
        if not names:
            raise ValueError("zip contains no files")

        # Detect a top-level directory prefix (e.g. "my-skill/SKILL.md").
        roots: set[str] = set()
        for n in names:
            parts = n.split("/", 1)
            roots.add(parts[0])
        single_root = len(roots) == 1 and "/" in names[0]

        if single_root:
            top = names[0].split("/", 1)[0]
            skill_name = preferred_name or top
        else:
            # Flat archive: derive name from preferred_name or first entry.
            skill_name = preferred_name or "uploaded-skill"

        if not is_valid_skill_name(skill_name):
            raise ValueError(f"invalid skill name derived from archive: {skill_name!r}")

        # Has a SKILL.md?
        skill_md_candidates = [n for n in names if n.endswith("SKILL.md")]
        if not skill_md_candidates:
            raise ValueError("zip must contain a SKILL.md file")

        dest_dir = self.workspace_skills / skill_name
        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # Compute destination path relative to the skill root.
                rel = info.filename
                if single_root:
                    rel = rel.split("/", 1)[1] if "/" in rel else rel
                # Normalize and guard against traversal.
                rel = rel.lstrip("/")
                if not rel or rel.startswith(".."):
                    continue
                target = (dest_dir / rel).resolve()
                try:
                    target.relative_to(dest_dir.resolve())
                except ValueError:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        finally:
            zf.close()

        if not (dest_dir / "SKILL.md").exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise ValueError("extracted archive has no SKILL.md at the skill root")
        return skill_name
