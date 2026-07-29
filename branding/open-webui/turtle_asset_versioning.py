"""Version only the importer chain of post-build patched JavaScript modules."""

from __future__ import annotations

import re
from pathlib import Path


MODULE_REFERENCE = re.compile(
    r"(?P<quote>[\"'`])"
    r"(?P<url>(?:/_app/immutable/|\./|\.\./)[^\"'`]+?\.js)"
    r"(?P=quote)"
)


def resolve_module_reference(
    path: Path,
    url: str,
    immutable_root: Path,
) -> Path | None:
    root = immutable_root.resolve()
    if url.startswith("/_app/immutable/"):
        resolved = root / url.removeprefix("/_app/immutable/")
    else:
        resolved = path.parent / url
    resolved = resolved.resolve()
    if resolved == root or root not in resolved.parents:
        return None
    return resolved


def version_references_to_modules(
    path: Path,
    targets: set[Path],
    *,
    immutable_root: Path,
    version: str,
) -> int:
    source = path.read_text(encoding="utf-8")
    replacements = 0

    def replacement(match: re.Match[str]) -> str:
        nonlocal replacements
        resolved = resolve_module_reference(
            path,
            match.group("url"),
            immutable_root,
        )
        if resolved not in targets:
            return match.group(0)
        replacements += 1
        return (
            f"{match.group('quote')}{match.group('url')}"
            f"?v={version}{match.group('quote')}"
        )

    updated = MODULE_REFERENCE.sub(replacement, source)
    if replacements:
        path.write_text(updated, encoding="utf-8")
    return replacements


def version_patched_module_chain(
    *,
    immutable_root: Path,
    index_path: Path,
    initial_targets: set[Path],
    version: str,
) -> set[Path]:
    if not re.fullmatch(r"[0-9a-f]{8,64}", version):
        raise ValueError("asset version must be a lowercase hexadecimal digest")

    root = immutable_root.resolve()
    modules = sorted(root.rglob("*.js"))
    versioned_modules = {target.resolve() for target in initial_targets}
    if not versioned_modules or any(
        not target.is_file() or root not in target.parents
        for target in versioned_modules
    ):
        raise ValueError("initial targets must be JavaScript files under immutable_root")

    while True:
        newly_versioned: set[Path] = set()
        for module_path in modules:
            if version_references_to_modules(
                module_path,
                versioned_modules,
                immutable_root=root,
                version=version,
            ):
                newly_versioned.add(module_path.resolve())
        previous_count = len(versioned_modules)
        versioned_modules.update(newly_versioned)
        if len(versioned_modules) == previous_count:
            break

    index_references = version_references_to_modules(
        index_path,
        versioned_modules,
        immutable_root=root,
        version=version,
    )
    if not index_references:
        raise RuntimeError(
            "the patched module importer chain does not reach index.html"
        )
    return versioned_modules
