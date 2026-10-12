"""the inventory of what crosses a service boundary, and its version.

A contract here is not "a pydantic model somewhere" -- it is the set of models a
provider serialises or validates on the wire, taken together with the version
constant it advertises. Grouping them matters: the version is bumped for the
contract, not for one model, so drift has to be judged against the whole set or
a field added to a nested model slips through while the top-level schema looks
unchanged.

Kept out of the test module so the release tooling and the tests read the same
list. A contract that exists on the wire and not here is invisible to every
check built on top, which is the failure this file is meant to make unlikely.

In the monorepo this list also held the contracts of the chat platform
(workflow.stream, rag.retrieve, prompt, dwh.export). This repository serves one:
`speech.api`, the surface of `speech-ms` the browser generates its types from.
"""

from __future__ import annotations

from typing import NamedTuple

from pydantic import BaseModel

from src.application.dto import api as speech_schemas
from src.domain.calls import contracts as speech_contracts
from src.presentation.http import (
    admin_routes as speech_admin,
    media_routes as speech_media,
    report_routes as speech_report,
)


class Contract(NamedTuple):
    name: str
    version: str
    models: tuple[type[BaseModel], ...]
    #: Which side validates. `request` means the provider validates what a
    #: caller sends; `response` means the caller validates what a provider
    #: returns. The direction decides which deployment order a breaking change
    #: hurts, so it is recorded rather than inferred.
    direction: str


CONTRACTS: tuple[Contract, ...] = (
    Contract(
        name="speech.api",
        version=speech_contracts.SPEECH_API_CONTRACT_VERSION,
        # The whole response surface of `speech-ms`, not the models that happen
        # to look interesting. The browser generates its types from this, and a
        # field quietly added to a nested model is exactly the change that
        # reaches production as a shape the front end never compiled against.
        models=(
            speech_schemas.MeResponse,
            speech_schemas.ClassifierCount,
            speech_schemas.StatsResponse,
            speech_schemas.QuoteModel,
            speech_schemas.SearchItemModel,
            speech_schemas.SearchResponse,
            speech_schemas.SegmentModel,
            speech_schemas.CallResponse,
            speech_schemas.OptionModel,
            speech_schemas.OptionsResponse,
            # Классификатор обращений для фильтра (SP-310).
            speech_schemas.ClassifierOption,
            speech_schemas.ClassifierResponse,
            # Оценка звонков по скрипту (2.3).
            speech_schemas.QualityStageModel,
            speech_schemas.QualityObjectionModel,
            speech_schemas.CallQualityResponse,
            speech_schemas.QualityBuckets,
            speech_schemas.QualityStageShare,
            speech_schemas.QualityObjectionShare,
            speech_schemas.QualityOutcomeShare,
            speech_schemas.QualityOperatorStage,
            speech_schemas.QualityOperatorScore,
            speech_schemas.QualityWeek,
            speech_schemas.QualitySummaryResponse,
            speech_schemas.QualityCallStage,
            speech_schemas.QualityCallItem,
            speech_schemas.QualityCallsResponse,
            # Дашборд (2.4).
            speech_schemas.DashboardTheme,
            speech_schemas.DashboardBucket,
            speech_schemas.DashboardSubtopic,
            speech_schemas.DashboardPeak,
            speech_schemas.DashboardSelection,
            speech_schemas.DashboardResponse,
            # Administration. Same contract and same version as the rest: the
            # front end generates one set of types from one `openapi.json`, and
            # a separate version for half of it would only tell it which half.
            # Классификатор обращений (SP-310).
            speech_admin.ClassifierNodeRow,
            speech_admin.ClassifierAdminResponse,
            speech_admin.ClassifierNodePatch,
            speech_admin.ProjectPatch,
            speech_admin.ProjectRow,
            speech_admin.ProjectsAdminResponse,
            speech_admin.SettingsPatch,
            speech_admin.SettingsResponse,
            speech_admin.LaneRow,
            speech_admin.DependencyRow,
            speech_admin.PipelineResponse,
            speech_report.ClassificationNode,
            speech_report.ClassificationResponse,
            speech_admin.JobRow,
            speech_admin.JobsResponse,
            speech_admin.AuditRow,
            speech_admin.AuditResponse,
            speech_admin.UserRow,
            speech_admin.UsersResponse,
            speech_admin.ActionResponse,
            # Кука для тега `<audio>` (SP-808): сама кука `HttpOnly` и фронту не
            # видна, поэтому срок он берёт из ответа — то есть это контракт.
            speech_media.AudioTicketResponse,
        ),
        direction="both",
    ),
)


def schema_of(contract: Contract) -> dict[str, object]:
    """The contract's wire shape, as JSON Schema, model by model."""
    return {model.__name__: model.model_json_schema() for model in contract.models}
