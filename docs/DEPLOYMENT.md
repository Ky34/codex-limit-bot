# Развёртывание Codex Limit Bot

## Статус

Автоматический production deploy подготовлен, но выключен. Job `deploy-production` запускается только после успешного `push` в `main` и только при repository variable `PRODUCTION_DEPLOY_ENABLED=true`. До отдельного разрешения эту переменную не создавать и не включать.

Workflow передаёт полный 40-символьный SHA. Сервер заново получает `origin/main` и отказывает, если переданный SHA больше не является текущей вершиной ветки.

## Размещение

- releases: `/opt/codex-limit-bot/releases/<sha>`;
- active symlink: `/opt/codex-limit-bot/current`;
- bare mirror: `/opt/codex-limit-bot/repo.git`;
- общий Codex CLI: `/opt/codex-limit-bot/shared/bin/codex`;
- состояние: `/var/lib/codex-limit-bot`;
- секреты: `/etc/codex-limit-bot.env`;
- runtime user: `codexbot`; deploy user: `codexdeploy`.

Скрипт блокирует параллельные запуски через `flock`, собирает release из точного commit и ставит зависимости из `requirements.lock` с проверкой hashes. Полные tests и compileall выполняются в CI. На сервере до и после переключения отдельный transient unit от `codexbot` безопасно проверяет Codex и Telegram через `getMe` и `getChat`, не отправляя сообщений. После запуска также проверяются стабильность listener, одноразовый service и timer. При ошибке возвращаются прежние units и ссылка. Текущий и предыдущий release сохраняются всегда; всего остаются пять каталогов.

## Однократная ручная подготовка сервера

Эти действия выполняются отдельно после разрешения на изменение production и резервной копии текущей установки.

1. Сверить текущий production с известным commit `main`; проверить `codexbot`, state, env и рабочую авторизацию. Проверить наличие Git, Python 3.12 с `venv`, systemd, `flock` из util-linux и GNU coreutils/findutils.
2. Создать releases, shared и root-owned bare mirror с origin `https://github.com/Ky34/codex-limit-bot.git`.
3. Перенести Codex CLI в shared path. Это должен быть обычный исполняемый root-owned файл.
4. Создать начальный release из проверенного commit: код, lock, три unit, `.venv` и `.release-sha`. Каталог сделать root-owned и неизменяемым для runtime user.
5. Создать атомарную ссылку `current`, установить units, выполнить daemon reload и проверить listener, одноразовый service и timer.
6. Установить `deploy/deploy-codex-limit-bot` в `/usr/local/sbin/deploy-codex-limit-bot` как `root:root`, режим `0755`.
7. Установить `deploy/check-systemd-policy` в `/usr/local/libexec/codex-limit-bot-check-systemd-policy` как `root:root`, режим `0755`.
8. Установить `deploy/ssh-entrypoint` в `/usr/local/libexec/codex-limit-bot-ssh-entrypoint` как `root:root`, режим `0755`.
9. Создать `codexdeploy` без пароля и доступа к данным бота. Root SSH key не использовать.
10. Добавить отдельный публичный deploy key в `~codexdeploy/.ssh/authorized_keys`:

   ```text
   restrict,command="/usr/local/libexec/codex-limit-bot-ssh-entrypoint" ssh-ed25519 PUBLIC_KEY_COMMENT
   ```

11. Установить sudoers-файл в `/etc/sudoers.d/codex-limit-bot`, режим `0440`, и проверить через `visudo -cf`.
12. Убедиться, что restricted SSH принимает только `deploy <40-символьный-sha>`, без forwarding, PTY, shell и других команд.
13. Создать GitHub Environment `production` и secrets `PROD_SSH_HOST`, `PROD_SSH_PORT`, `PROD_SSH_USER` (`codexdeploy`), `PROD_SSH_PRIVATE_KEY`, `PROD_SSH_KNOWN_HOSTS`. Fingerprint сервера сверить по доверенному каналу.
14. Защитить `main` обязательным CI check.
15. Только после отдельного разрешения создать repository variable `PRODUCTION_DEPLOY_ENABLED=true`. Без неё production job пропускается.

## Проверка и откат

После первого разрешённого запуска сверить путь `current` с SHA опубликованного `origin/main`, активность listener и timer и `Result=success` у одноразового service. Проверить журналы двух services за время deploy.

Автоматический откат работает при штатной ошибке deploy-процесса. Отключение питания может потребовать ручного выбора последнего исправного release, восстановления его units, атомарной замены `current`, daemon reload и запуска служб. Пять releases не заменяют резервную копию state и secrets.

## Секреты и границы

В Git не входят Telegram token, SSH private key, env, state, `.codex` и файлы авторизации. Deploy не меняет state, secrets и общий Codex CLI. Обновление CLI выполняется отдельной согласованной операцией.
