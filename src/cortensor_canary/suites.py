"""Suite loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .scoring import ExpectationError, score_answer


class SuiteError(ValueError):
    """Invalid or missing suite."""


@dataclass(frozen=True)
class Case:
    id: str
    prompt: str
    expect: dict[str, Any]
    system: str = ""
    max_tokens: int = 256
    tags: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, index: int, data: dict[str, Any], default_max_tokens: int) -> "Case":
        if not isinstance(data, dict):
            raise SuiteError(f"case #{index}: must be an object")
        case_id = str(data.get("id") or "").strip()
        prompt = str(data.get("prompt") or "").strip()
        expect = data.get("expect")
        if not case_id:
            raise SuiteError(f"case #{index}: id is required")
        if not prompt:
            raise SuiteError(f"case {case_id}: prompt is required")
        if not isinstance(expect, dict):
            raise SuiteError(f"case {case_id}: expect must be an object")
        tags = data.get("tags") or []
        if not isinstance(tags, list):
            raise SuiteError(f"case {case_id}: tags must be a list")
        try:
            score_answer("", expect)
        except ExpectationError as exc:
            raise SuiteError(f"case {case_id}: {exc}") from exc
        return cls(
            id=case_id,
            prompt=prompt,
            expect=dict(expect),
            system=str(data.get("system") or ""),
            max_tokens=int(data.get("max_tokens") or default_max_tokens),
            tags=tuple(str(tag) for tag in tags),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "prompt": self.prompt,
            "expect": self.expect,
            "max_tokens": self.max_tokens,
        }
        if self.system:
            payload["system"] = self.system
        if self.tags:
            payload["tags"] = list(self.tags)
        return payload


@dataclass(frozen=True)
class Suite:
    name: str
    description: str
    version: str
    cases: tuple[Case, ...]
    path: str = ""

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for case in self.cases:
            if case.id in seen:
                raise SuiteError(f"suite {self.name!r}: duplicate case id {case.id!r}")
            seen.add(case.id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "path": self.path,
            "cases": len(self.cases),
        }

    @property
    def tags(self) -> list[str]:
        return sorted({tag for case in self.cases for tag in case.tags})


def data_dir() -> Path:
    return Path(__file__).resolve().parent / "data" / "suites"


def search_paths(ref: str) -> Iterable[Path]:
    candidate = Path(ref)
    if candidate.suffix == ".json" or candidate.exists():
        yield candidate
    yield data_dir() / f"{ref}.json"
    yield Path.cwd() / "suites" / f"{ref}.json"
    yield Path.cwd() / f"{ref}.json"


def find_suite(ref: str) -> Path:
    for path in search_paths(ref):
        if path.is_file():
            return path
    available = sorted(p.stem for p in data_dir().glob("*.json"))
    raise SuiteError(f"suite {ref!r} not found; available: {', '.join(available) or 'none'}")


def load_suite(ref: str | Path) -> Suite:
    path = find_suite(str(ref))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SuiteError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SuiteError(f"{path}: suite must be a JSON object")
    name = str(payload.get("name") or path.stem).strip()
    default_max_tokens = int(payload.get("default_max_tokens") or 256)
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise SuiteError(f"{path}: 'cases' must be a non-empty list")
    cases = tuple(
        Case.from_mapping(index, item, default_max_tokens)
        for index, item in enumerate(raw_cases, start=1)
    )
    return Suite(
        name=name,
        description=str(payload.get("description") or ""),
        version=str(payload.get("version") or "1.0"),
        cases=cases,
        path=str(path),
    )


def list_suites() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(data_dir().glob("*.json")):
        try:
            suite = load_suite(path)
        except SuiteError as exc:  # pragma: no cover - broken repo state
            result.append({"name": path.stem, "error": str(exc)})
            continue
        result.append(
            {
                "name": suite.name,
                "description": suite.description,
                "version": suite.version,
                "cases": len(suite.cases),
                "tags": suite.tags,
                "path": str(path),
            }
        )
    return result
