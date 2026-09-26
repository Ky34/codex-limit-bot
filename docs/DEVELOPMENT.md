# Разработка Codex Limit Bot

Этот документ содержит технические шаги для локальной разработки. Назначение, функции и ограничения бота описаны в [README](../README.md); действующие правила работы — в [AGENTS.md](../AGENTS.md), текущий переносимый контекст — в [PROJECT_STATE.md](../PROJECT_STATE.md).

## Подготовка окружения

Нужен Python 3.12 или новее. Прямые зависимости перечислены в `requirements.txt`. Production-набор для CPython 3.12 и Linux x86_64 зафиксирован с hashes в `requirements.lock`. Рабочая ветка для разработки и ручной синхронизации между Windows-ПК и MacBook — `dev`.

При первом получении проекта:

```sh
git clone https://github.com/Ky34/codex-limit-bot.git
cd codex-limit-bot
git switch --track origin/dev
```

На macOS:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

На Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Проверки CI

Workflow `.github/workflows/ci-deploy.yml` проверяет pull request в `main` и каждый push в `dev` или `main`. Он устанавливает зависимости по lock-файлу, запускает tests и compileall, сверяет lock с прямыми зависимостями, проверяет полную systemd policy, Bash через ShellCheck, а sudoers — через `visudo`. При изменении `requirements.txt` нужно заново получить совместимые Linux wheels, пересчитать hashes и проверить установку с `--require-hashes`.

## Синхронизация, публикация и deploy

Перед работой проверь состояние рабочей копии, безопасно получи актуальную `dev` и прочитай `PROJECT_STATE.md`. Перед переходом на другое устройство зафиксируй существенные изменения контекста и отправь их в `dev`. При конфликтах или незавершённых локальных изменениях остановись и разберись с ними, не перезаписывая чужую работу.

Активный production-экземпляр находится на собственном VPS пользователя и обслуживается через `ssh my-vps`; старый VPS не является целью. Production deploy подготовлен, но выключен до ручной серверной установки и явного включения. Схема и оставшиеся шаги описаны в [DEPLOYMENT.md](DEPLOYMENT.md). После включения разрешённый push конкретного проверенного commit в `main` автоматически разворачивает только тот же SHA. Обычная синхронизация через `dev` production не обновляет. Не запускай локальный Telegram listener с рабочим токеном параллельно с серверным. Секреты и рабочие данные сервера не хранятся в Git.

Локальное размещение Windows-проекта описано в [LOCAL-LAYOUT.md](../LOCAL-LAYOUT.md).
