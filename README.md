# Ufanet Intercom

Неофициальная интеграция Ufanet для Home Assistant. Ниже сначала приведена русская документация; краткая английская версия следует после неё.

> **Важно:** это независимый community-проект. Он не связан с Ufanet, не одобрен компанией и не поддерживается её службой поддержки.

> **Версия 2.0.0rc6 — release candidate.** Она предназначена для контролируемой установки с резервной копией и проверкой обнаруженных входов перед созданием автоматизаций. После сетевой ошибки или неизвестного результата не повторяйте открытие вручную как диагностический тест. Пошаговый порядок внедрения и возврата: [Controlled rollout and rollback](docs/controlled-rollout-and-rollback.md).

## Русский

### Возможности

- В форме настройки запрашиваются только договор (логин) Ufanet и пароль.
- Авторизация выполняется через проверенный endpoint, соответствующий мобильному приложению Ufanet.
- После авторизации интеграция только для чтения обнаруживает все поддерживаемые общие домофоны семейства **shared SKUD** в учётной записи.
- Для каждого обнаруженного домофона создаются отдельное устройство Home Assistant и стандартная сущность **Button** с действием `button.press`.
- Для каждого доверенного домофона создаётся нейтральный Binary Sensor **Call detected / Звонок обнаружен**. Это короткий импульс наблюдения строки истории звонков, а не утверждение, что вызов сейчас активен.
- Для **каждого доверенного домофона с доступной камерой** автоматически создаётся сущность **Camera** внутри того же устройства. Интеграция получает camera binding и media-сервер из текущей учётной записи, выдаёт Home Assistant только loopback RTSP URL с непрозрачным ключом и передаёт поток по TCP; краткоживущие provider-токены остаются в памяти и не попадают в entity state или аргументы ffmpeg. Frigate/go2rtc, ручные URL, camera ID и токены не требуются.
- «Автовахтёр» пока не включается: текущий polling истории звонков не является надёжным сигналом активного вызова и не может безопасно запускать открытие двери.

Первоначальная настройка показывает только общее количество найденных входов и требует явного подтверждения перед созданием записи: пользователь принимает риск физического срабатывания и доверяет всем обнаруженным привязкам. Новые или изменившиеся привязки при последующих обнаружениях помещаются в карантин и не получают автоматического доверия. Чтобы принять их все сразу, откройте **Параметры** записи интеграции, проверьте количество новых и изменённых привязок и явно подтвердите принятие.

Отображаемые имена поступают от провайдера и сохраняются как имена устройств и сущностей Home Assistant. Они могут быть похожи на адреса и оставаться в реестрах, истории, резервных копиях и связанных удалённых системах даже после изменения данных у провайдера.

Кнопка открытия имеет фиксированную семантику кратковременной команды: она отправляет запрос на открытие, но не изображает состояние физического замка. Ufanet не предоставляет надёжный датчик положения двери, поэтому доступная сущность остаётся в состоянии `unknown`, а не переключается между `locked` и `unlocked`.

История звонков опрашивается одним последовательным poller на аккаунт раз в 3 секунды (до 28 800 GET в сутки на аккаунт). После запуска сначала выполняется baseline без импульса; при сбое, потере непрерывности первой страницы или большом перерыве поток становится недоступен и заново базелайнится без сигнала. Импульс означает только, что свежая строка была замечена; задержка возможна из-за polling, нет гарантии активного звонка или его окончания. Имена от провайдера (`custom_name`, затем `string_view`, затем `address`) сохраняются Home Assistant и могут содержать адресные персональные данные. SIP-аккаунты и SIP-провижининг интеграция не создаёт.

### Поддерживаемые устройства и ограничения

Поддерживается только семейство общих домофонов **shared SKUD**, доступных текущей учётной записи. Отдельное семейство устройств, модулей и шлагбаумов не поддерживается, пока его связь между инвентарём и физической командой не будет независимо проверена.

Интеграция не добавляет MQTT, распознавание лиц, голосовое управление или произвольную обработку видеопотока. Камеры обнаруживаются по данным каждой добавленной учётной записи и доступны через встроенный loopback-only RTSP relay; он проверяет provider-owned media hosts и скрывает краткоживущие подписанные URL. Функционал автовахтёра не включён до появления отдельного realtime-сигнала вызова и безопасной политики подтверждения.

### Безопасность и приватность

Учётные данные сохраняются локально в стандартной записи конфигурации Home Assistant и используются только для обращения к Ufanet. Они также могут попасть в резервные копии Home Assistant: защищайте backups шифрованием, доступом и политикой хранения так же строго, как пароль. Не публикуйте договор, пароль, токены, provider ID, адрес, данные камеры или сырую диагностику.

Если Home Assistant доступен извне или подключён к голосовому ассистенту, ограничьте доступ к `button.press`: такая интеграция расширяет поверхность физического доступа.

Перед физической командой интеграция выполняет свежую проверку доступного инвентаря. После начала физического запроса повторов нет: при сетевой ошибке или тайм-ауте результат считается неизвестным, чтобы случайно не отправить команду второй раз. Настройка, перезагрузка и read-only обновление не должны открывать дверь.

При обновлении старой записи версии 1.x интеграция локально переводит её в формат v2 без сетевого запроса и без команды открытия. Все прежние физические привязки отбрасываются, запись остаётся в карантине, а работа сущностей блокируется до явного принятия актуальных привязок через **Параметры**. Миграция не создаёт автоматического доверия к прежним или вновь найденным входам.

Управление физическим доступом несёт реальный риск. Проверяйте автоматизации и используйте минимально необходимые права. Уязвимости, связанные с физическим доступом, сообщайте приватно по инструкции в [SECURITY.md](SECURITY.md).

### Установка через HACS

1. Откройте HACS и раздел **Integrations**.
2. Откройте меню пользовательских репозиториев и введите **GitHub URL этого репозитория**.
3. Выберите категорию **Integration** и добавьте репозиторий.
4. В меню репозитория включите **Show beta versions**, затем выберите **Need a different version?** и установите точный тег **v2.0.0rc6**. Не устанавливайте RC с ветки по умолчанию.
5. Перезапустите Home Assistant и до настройки или физической проверки убедитесь в HACS, что установленная версия — именно **2.0.0rc6**.
6. Откройте **Настройки → Устройства и службы → Добавить интеграцию**, выберите **Ufanet Intercom** и введите договор (логин) и пароль.

## English

Ufanet Intercom is an unofficial community integration for Home Assistant. It is not affiliated with, endorsed by, or supported by Ufanet.

> **Version 2.0.0rc6 is a release candidate.** Install it only through a controlled rollout with a backup, and review every discovered entrance before creating automations. After a network error or unknown outcome, do not retry opening as a diagnostic action. Follow [Controlled rollout and rollback](docs/controlled-rollout-and-rollback.md).

### Behavior and scope

The config flow asks only for the Ufanet contract/login and password and authenticates through the verified mobile-style authentication endpoint. It then performs read-only discovery of every supported **shared SKUD** intercom available to the account. Each intercom becomes one Home Assistant Device with one standard Button entity supporting `button.press`.
Each trusted intercom also gets a neutral **Call detected** binary sensor. It is a short observed-history pulse, not an active-ring state.
Each trusted intercom with an available provider camera also gets a **Camera** entity inside the same Device. Camera bindings and the active media server are discovered from that account. Home Assistant receives only a loopback RTSP URL containing an opaque key and consumes it over TCP; short-lived provider tokens stay in process memory and never enter entity state or ffmpeg arguments. No Frigate/go2rtc setup, manual URL, camera ID, or token is required.
The "auto-concierge" feature is intentionally not enabled yet: history polling is not a reliable active-call signal and must not trigger a door-opening command.

Initial setup shows only aggregate discovery counts and requires explicit acknowledgement before creating the entry: the user accepts the physical-actuation warning and trusts all currently discovered bindings. Newly discovered or changed bindings are quarantined and are not automatically trusted. To adopt all pending bindings, open the integration entry's **Options**, review the aggregate new and changed counts, and explicitly confirm adoption.

Provider-supplied display names persist as Home Assistant entity and device names. They may resemble addresses and can remain in registries, history, backups, remote UI, or voice-assistant metadata even if the provider later changes them.

The Button represents a momentary open request, not a physical lock sensor. It has no persistent locked/unlocked state. Before pressing it, the integration refreshes its read-only view of the account. Once the physical request begins, it is never retried, and an ambiguous transport result remains unknown.

Call history is polled by one serialized account-level worker every 3 seconds (up to 28,800 history GETs per account per day). Startup first establishes a silent baseline. Failures, continuity/anchor loss, recovery, or a conservative observation gap make the sensor unavailable and trigger a fresh silent baseline. The pulse only means a fresh history row was observed; polling latency is possible, and it does not guarantee that a call is currently active or indicate when it ended. Provider-derived names (`custom_name`, then `string_view`, then `address`) persist in Home Assistant and may contain address-like personal data. No SIP account provisioning is performed.

Only the shared SKUD intercom family is supported. The separate device/module/barrier family is unsupported.
The integration does not add MQTT, face recognition, voice control, or arbitrary video processing. Cameras are discovered independently for every added account and are exposed through an embedded loopback-only RTSP relay that validates provider-owned media hosts and redacts short-lived signed URLs. Users cannot provide arbitrary IDs, URLs, door selectors, or physical targets. Auto-concierge remains disabled until a separate realtime call signal and a safe confirmation policy exist.

Credentials remain in Home Assistant's local config-entry storage and may therefore be included in Home Assistant backups. Protect backup encryption, access, and retention as carefully as the password itself. Never post credentials, tokens, contract values, provider IDs, addresses, camera data, or raw diagnostics publicly.

When a legacy 1.x entry is upgraded, local v2 migration performs no network request and no physical command. It discards every legacy physical binding, quarantines the entry, and blocks entities until current bindings are explicitly adopted through **Options**. Migration never auto-trusts legacy or newly discovered entrances. A prior `lock.*` registry row is removed as a domain-migration tombstone; the replacement `button.*` row is created with the same opaque unique ID and device relationship, but the old domain-specific entity ID cannot be preserved.

If Home Assistant is remotely accessible or connected to a voice assistant, restrict access to `button.press`; those features expand the physical-access surface. Report physical-access vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

### Install with HACS

1. Open **HACS → Integrations → Custom repositories**.
2. Enter **this repository's GitHub URL** and select **Integration**.
3. Add the repository. In its menu, enable **Show beta versions**, select **Need a different version?**, and install the exact **v2.0.0rc6** tag. Do not install the RC from the mutable default branch.
4. Restart Home Assistant and verify the installed version in HACS is exactly **2.0.0rc6** before configuration or any physical test.
5. Add **Ufanet Intercom** from **Settings → Devices & services** and enter the contract/login and password.

## Development checks

The local test suite uses synthetic data and must not contact Ufanet or perform a physical action.

```bash
uv sync --locked --dev
uv run ruff check . --isolated --select E4,E7,E9,F
uv run ruff format --check . --isolated
uv run pytest
PYTHONPATH=. uv run --no-project --python 3.14.5 --with homeassistant==2026.7.4 --with PyTurboJPEG==1.8.3 --with av==17.0.1 --with numpy==2.3.2 python tests/ha_entity_registry_probe.py
PYTHONPATH=. uv run --no-project --python 3.14.5 --with homeassistant==2026.8.1 --with PyTurboJPEG==1.8.3 --with av==17.0.1 --with numpy==2.3.2 python tests/ha_entity_registry_probe.py
```

Licensed under the [MIT License](LICENSE).
