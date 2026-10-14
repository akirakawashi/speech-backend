"""DTO подготовки запроса и фактического выполнения LLM внутри бэкенда."""

import hashlib
import json
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

ModelScenario = Literal["analysis", "reason", "free_classification", "operator_compare"]
ModelProfile = Literal["production", "candidate"]
Sha256 = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
NonEmptyText = Annotated[str, Field(min_length=1, strict=True)]


class ModelMessage(BaseModel):
    """Роль и текст сообщения; содержимое разговора не выводится в repr."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, repr=False)


class ModelPreparationRequest(BaseModel):
    """Выбрать сценарий и передать его именованные входы без HTTP-объектов."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    contract_version: Literal["1.0"] = "1.0"
    scenario: ModelScenario
    profile: ModelProfile = "production"
    inputs: dict[str, JsonValue] = Field(repr=False)


class PreparedModelRequest(BaseModel):
    """Готовый запрос; адрес модели и секреты остаются у исполнителя."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    contract_version: Literal["1.0"]
    scenario: ModelScenario
    profile: ModelProfile
    prompt_version: str = Field(min_length=1)
    prompt_digest: Sha256
    model: str = Field(min_length=1)
    messages: tuple[ModelMessage, ...] = Field(min_length=1, repr=False)
    temperature: float = Field(ge=0, le=2)
    max_tokens: int = Field(ge=1, le=32768)


class PromptVariable(BaseModel):
    """Имя, обязательность и ограничение входа одного сценария."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    required: bool = True
    max_length: int | None = Field(default=None, ge=1)


class PromptTemplate(BaseModel):
    """Версия промпта, сообщения и явные переменные для их подготовки."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    scenario: ModelScenario
    version: str
    variables: tuple[PromptVariable, ...]
    messages: tuple[ModelMessage, ...]

    def fingerprint(self) -> str:
        """Вернуть SHA-256 шаблона до подстановки разговора.

        Returns:
            Хеш канонического JSON со сценарием, версией, переменными и
            сообщениями. Порядок ключей не значим; порядок сообщений значим.
        """
        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ModelExecutionMetadata(BaseModel):
    """Факты одного HTTP-вызова без текста, секретов и сведений из ответа LLM.

    Имя обслужившей модели и её дайджест заполняются только из явных полей
    протокола провайдера. Отсутствующие значения остаются неизвестными.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: UUID
    scenario: ModelScenario
    profile: ModelProfile
    requested_model: NonEmptyText
    served_model: NonEmptyText | None = None
    model_digest: NonEmptyText | None = None
    prompt_version: NonEmptyText
    prompt_digest: Sha256
    request_fingerprint: Sha256
    temperature: float = Field(ge=0, le=2)
    max_tokens: int = Field(ge=1, le=32768, strict=True)
    started_at: AwareDatetime
    finished_at: AwareDatetime

    @model_validator(mode="after")
    def validate_time(self) -> Self:
        """Вернуть факты вызова; время ответа до начала вызывает ValueError."""
        if self.finished_at < self.started_at:
            raise ValueError("model response precedes the request")
        return self


class ModelExecutionResult(BaseModel):
    """Непроверенный ответ модели вместе с фактами породившего его запроса."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    outputs: JsonValue = Field(repr=False)
    execution: ModelExecutionMetadata
