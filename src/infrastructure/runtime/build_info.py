"""What is actually running here, answerable over HTTP.

Until now nothing in a container could say which commit it was built from. With
one shared image that was merely inconvenient -- everything was the same build.
With a per-service image per deployable it stops being answerable at
all by inspection: five images, five release cadences, and the only honest way
to know what a running service is is to ask it.

`/version` therefore reports three separate things, and the difference between
them matters when a rolling deployment goes wrong:

* **commit and build time** -- identity of the artifact. Injected at build time,
  never computed at runtime: reading git from inside a container would report
  whatever source happens to be mounted, which is exactly the lie this is meant
  to prevent. Unset means "not built by the release pipeline", reported as
  `unknown` rather than guessed.
* **service** -- which deployable this image is, so a mislabelled image cannot
  hide behind a correct commit.
* **contract versions** -- what this build can speak. These come from the
  constants the code actually serialises, so they cannot drift from behaviour;
  a caller comparing them against its own is doing version negotiation with the
  real answer.
"""

from __future__ import annotations

from typing import Final

from src.application.dto.system import VersionInfo as VersionInfo

UNKNOWN: Final[str] = "unknown"

# Set by the image build (`--build-arg`), not by the runtime environment. Named
# with the project prefix so an unrelated CI variable cannot claim to describe
# this artifact.
COMMIT_ENV: Final[str] = "AICISTENT_GIT_COMMIT"
BUILD_TIME_ENV: Final[str] = "AICISTENT_BUILD_TIME"


def build_version_info(
    service: str,
    contracts: dict[str, str] | None = None,
    *,
    commit: str = UNKNOWN,
    build_time: str = UNKNOWN,
) -> VersionInfo:
    """Из явных метаданных сборки и версий контрактов вернуть DTO ответа /version."""
    return VersionInfo(
        service=service,
        commit=commit or UNKNOWN,
        build_time=build_time or UNKNOWN,
        contracts=dict(contracts or {}),
    )
