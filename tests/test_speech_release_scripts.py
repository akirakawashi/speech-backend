"""Сборка, фрагмент окружения и compose обязаны называть одни и те же образы.

Три файла в двух репозиториях держат один список, и держат его руками:
`build-and-save-images.sh` решает, что собрать; `images-<tag>.env` называет
собранное; `docker-compose.yml` читает эти имена. Разъезд любых двух из них
обнаруживается на узле — после того, как архив на 450 МБ уже уехал, — и
выглядит как «image not found» либо, хуже, как молчаливый откат к `:local`:
стек поднимается на образе, которого там быть не должно.

Проверки стоят в наборе бэкенда, а не рядом со скриптами: своего набора у
скриптов выпуска нет, ломаются они от правки в соседнем репозитории, а
замечает это тот, кто трогает этот.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

RELEASE = Path(__file__).resolve().parents[2] / "aicistent-infrastructure" / "scripts" / "release"
BUILD_SCRIPT = RELEASE / "build-and-save-images.sh"
DOCKERFILE = Path("Dockerfile")
COMPOSE_FILE = Path("docker-compose.yml")
ENV_EXAMPLE = Path(".env.example")

pytestmark = pytest.mark.skipif(
    not BUILD_SCRIPT.exists(), reason=f"репозиторий инфраструктуры не рядом: {BUILD_SCRIPT}"
)


def _script() -> str:
    return BUILD_SCRIPT.read_text(encoding="utf-8")


def _backend_targets() -> tuple[str, ...]:
    """Значения массива `BACKEND_TARGETS` — по одной цели на строку."""
    body = re.search(r"BACKEND_TARGETS=\(\s*(.*?)\s*\)", _script(), re.S)
    assert body is not None, "массив BACKEND_TARGETS не найден"
    return tuple(line.strip() for line in body.group(1).splitlines() if line.strip())


def _fragment_images() -> dict[str, str]:
    """Строки `ИМЯ=образ:$TAG`, которые скрипт пишет в фрагмент окружения."""
    pairs = re.findall(r'echo "([A-Z_]+_IMAGE)=([a-z0-9-]+):\$TAG"', _script())
    return dict(pairs)


def _built_images() -> set[str]:
    """Образы, которые скрипт действительно собирает.

    Цели бэкенда собираются из одного Dockerfile по шаблону `aicistent-<цель>`;
    остальные объявлены отдельными строками `docker build -t`.
    """
    built = {f"aicistent-{target}" for target in _backend_targets()}
    built |= set(re.findall(r'_image="([a-z0-9-]+):\$TAG"', _script()))
    return built


def _dockerfile_stages() -> set[str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    return set(re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", text, re.M))


@pytest.mark.parametrize("target", _backend_targets() if BUILD_SCRIPT.exists() else [])
def test_every_built_target_exists_in_the_dockerfile(target: str) -> None:
    """Опечатка в цели обнаруживается сборкой — через час и восемь образов.

    `docker build --target` на несуществующей цели отказывает сразу, но сборка
    к этому моменту уже идёт долго, а запускают её не каждый день и обычно в
    день выкатки.
    """
    assert target in _dockerfile_stages(), (
        f"цель {target!r} собирается скриптом, но стадии с таким именем в Dockerfile нет"
    )


def test_the_speech_service_and_worker_are_built() -> None:
    """Обе цели речи — в списке сборки.

    Отдельной проверкой, а не общим списком: их добавляли последними, и потерять
    их при следующей правке массива легче всего.
    """
    targets = _backend_targets()
    assert "speech-ms" in targets
    assert "speech-worker" in targets


def test_every_name_in_the_fragment_is_actually_built() -> None:
    """Фрагмент не должен называть образ, которого никто не собрал.

    На узле это `image not found` после того, как архив уже скопирован: сборку
    придётся повторять целиком, а окно выкатки уже потрачено.
    """
    built = _built_images()
    for variable, image in _fragment_images().items():
        assert image in built, f"{variable} называет {image!r}, но скрипт его не собирает"


def test_every_built_image_reaches_the_fragment() -> None:
    """И наоборот: собранный, но неназванный образ — тихая потеря.

    Compose возьмёт своё умолчание `:local`, и на узле поднимется либо
    вчерашний образ, либо ничего. Первое хуже: стек работает и выглядит
    выкаченным.
    """
    named = set(_fragment_images().values())
    missing = _built_images() - named
    assert not missing, f"собираются, но в фрагмент не попадают: {sorted(missing)}"


def _compose_image_variables() -> set[str]:
    """Переменные образов, которые читают службы речи в compose."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    found: set[str] = set()
    for name, service in compose["services"].items():
        if not name.startswith("speech-"):
            continue
        match = re.match(r"\$\{([A-Z_]+)(:-.*)?\}$", str(service.get("image", "")))
        if match:
            found.add(match.group(1))
    return found


def test_compose_reads_only_variables_the_build_writes() -> None:
    """Службы речи читают ровно те имена, которые пишет сборка.

    Исключение — `SPEECH_MINIO_IMAGE`: MinIO берётся из публичного реестра,
    мы его не собираем, и требовать его от сборки значило бы требовать сборки
    чужого образа.
    """
    written = set(_fragment_images())
    for variable in _compose_image_variables() - {"SPEECH_MINIO_IMAGE"}:
        assert variable in written, (
            f"compose читает {variable}, но сборка её не пишет — на узле подставится "
            "умолчание `:local`"
        )


def test_the_frontend_image_is_declared_where_the_operator_looks() -> None:
    """Образ фронтенда речи объявлен в шаблоне окружения.

    Переменная, которой нет в `.env.example`, не существует для того, кто
    заполняет `.env` руками: он не знает, что её нужно задать.
    """
    declared = ENV_EXAMPLE.read_text(encoding="utf-8")
    for variable in ("SPEECH_MS_IMAGE", "SPEECH_WORKER_IMAGE", "SPEECH_FRONTEND_IMAGE"):
        assert re.search(rf"^{variable}=", declared, re.M), f"{variable} нет в .env.example"


def test_the_speech_repository_is_checked_before_building() -> None:
    """Грязное дерево фронтенда речи обязано останавливать сборку.

    Коммит запекается в образ и показывается наружу; образ из некоммиченного
    дерева называет коммит, который его не описывает. Проверка существует для
    трёх репозиториев — четвёртый добавлен последним и выпасть может так же
    незаметно, как появился.
    """
    script = _script()
    checked = re.findall(r'for dir in ([^;]+); do\n\s+if \[ -n "\$\(git -C', script)
    assert checked, "цикл проверки чистоты деревьев не найден"
    assert "$SPEECH" in checked[0], "дерево фронтенда речи на чистоту не проверяется"
