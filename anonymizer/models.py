from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Decision(StrEnum):
    AUTO = "AUTO"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


class FileStatus(StrEnum):
    CLEAN = "CLEAN"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(slots=True)
class Person:
    person_id: str
    person_token: str
    full_name: str
    surname: str = ""
    given_name: str = ""
    patronymic: str = ""
    department: str = ""
    title: str = ""
    email: str = ""
    employee_number: str = ""
    aliases: list[str] = field(default_factory=list)

    def safe_label(self) -> str:
        extras = ", ".join(x for x in (self.department, self.title) if x)
        return f"{self.full_name}{' — ' + extras if extras else ''}"


@dataclass(slots=True)
class Finding:
    finding_id: str
    file: str
    location: str
    category: str
    original: str
    start: int
    end: int
    decision: Decision
    confidence: float
    person_id: str | None = None
    candidates: list[str] = field(default_factory=list)
    reason: str = ""
    context: str = ""
    permanently_removed: bool = False
    key: str = ""          # устойчивая идентичность сущности: одинаковый ключ — один и тот же токен


@dataclass(slots=True)
class FileResult:
    relative_path: str
    format: str
    status: FileStatus
    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    output_path: str = ""
    integrity: dict[str, object] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)     # сведения, не требующие действий

    @property
    def replacements(self) -> int:
        return sum(1 for f in self.findings if f.decision == Decision.AUTO)


@dataclass(slots=True)
class Settings:
    personal_data: bool = True
    secrets: bool = True
    business_confidential: bool = False
    scrub_metadata: bool = True
    anonymize_filenames: bool = True
    learn_names: bool = True
    inspect_embedded: bool = True
    retain_secrets_for_restore: bool = False
    organizations: bool = True
    geo: bool = True
    domains: bool = True
    filenames: bool = True
    numbers: bool = False
    strict: bool = False
    suggest: bool = False
    hide_terms: list[str] = field(default_factory=list)
    keep_terms: list[str] = field(default_factory=list)

    @classmethod
    def persons_only(cls, **overrides) -> "Settings":
        """Только люди и структурированные ПДн: для проверок распознавания ФИО в отрыве от организаций и городов."""
        return cls(organizations=False, geo=False, domains=False, filenames=False, **overrides)


@dataclass(slots=True)
class Project:
    project_id: str
    project_tag: str
    source_root: Path
    settings: Settings
    people: list[Person] = field(default_factory=list)
    files: list[FileResult] = field(default_factory=list)
    overrides: dict[str, str] = field(default_factory=dict)
    # Метка прогона. Номера в плейсхолдерах раздаются заново при каждом обезличивании, поэтому
    # два прогона одного проекта не имеют права делить пространство меток: иначе ключ второго
    # прогона молча восстановит в файлах первого чужие имена.
    run_tag: str = ""

    @property
    def token_tag(self) -> str:
        """Метка, которая попадает в плейсхолдеры и в имя файла ключа."""
        return self.run_tag or self.project_tag


@dataclass(slots=True)
class Replacement:
    token: str
    category: str
    original: str | None
    person_id: str | None
    file: str
    location: str
    source_hash: str

