# Ufanet Intercom

Неофициальная интеграция Ufanet для Home Assistant. Ниже сначала приведена русская документация; краткая английская версия следует после неё.

> **Важно:** это независимый community-проект. Он не связан с Ufanet, не одобрен компанией и не поддерживается её службой поддержки.

> **Версия 2.1.0b7 — бета-версия.** Она предназначена для контролируемой установки с резервной копией и проверкой обнаруженных входов перед созданием автоматизаций. После сетевой ошибки или неизвестного результата не повторяйте открытие вручную как диагностический тест. Пошаговый порядок внедрения и возврата: [Controlled rollout and rollback](docs/controlled-rollout-and-rollback.md).

## Русский

### Возможности

- В форме настройки запрашиваются только договор (логин) Ufanet и пароль.
- Авторизация выполняется через проверенный endpoint, соответствующий мобильному приложению Ufanet.
- После авторизации интеграция только для чтения обнаруживает все поддерживаемые общие домофоны семейства **shared SKUD** в учётной записи.
- Для каждого обнаруженного домофона создаются отдельное устройство Home Assistant и стандартная сущность **Button** с действием `button.press`.
- Для каждого доверенного домофона создаётся нейтральный Binary Sensor **Call detected / Звонок обнаружен**. Это короткий импульс наблюдения строки истории звонков, а не утверждение, что вызов сейчас активен.
- Для выбранных доверенных домофонов с камерой можно включить отдельный Binary Sensor **Code phrase detected / Кодовая фраза распознана**. Он включается ровно на 5 секунд после точного совпадения с одной из настроенных фраз и **не открывает дверь**.
- Для **каждого доверенного домофона с доступной камерой** автоматически создаётся сущность **Camera** внутри того же устройства. Интеграция получает camera binding и media-сервер из текущей учётной записи, выдаёт Home Assistant только loopback RTSP URL с непрозрачным ключом и передаёт поток по TCP; краткоживущие provider-токены остаются в памяти и не попадают в entity state или аргументы ffmpeg. Frigate/go2rtc, ручные URL, camera ID и токены не требуются.
- «Автовахтёр» пока не включается: текущий polling истории звонков не является надёжным сигналом активного вызова и не может безопасно запускать открытие двери.

Первоначальная настройка показывает только общее количество найденных входов и требует явного подтверждения перед созданием записи: пользователь принимает риск физического срабатывания и доверяет всем обнаруженным привязкам. Новые или изменившиеся привязки при последующих обнаружениях помещаются в карантин и не получают автоматического доверия. Чтобы принять их все сразу, откройте **Параметры → Обновить список домофонов** записи интеграции, проверьте количество новых и изменённых домофонов и явно подтвердите принятие.

Отображаемые имена поступают от провайдера и сохраняются как имена устройств и сущностей Home Assistant. Они могут быть похожи на адреса и оставаться в реестрах, истории, резервных копиях и связанных удалённых системах даже после изменения данных у провайдера.

Кнопка открытия имеет фиксированную семантику кратковременной команды: она отправляет запрос на открытие, но не изображает состояние физического замка. Ufanet не предоставляет надёжный датчик положения двери, поэтому доступная сущность остаётся в состоянии `unknown`, а не переключается между `locked` и `unlocked`.

История звонков опрашивается одним последовательным poller на аккаунт раз в 3 секунды (до 28 800 GET в сутки на аккаунт). После запуска сначала выполняется baseline без импульса; при сбое, потере непрерывности первой страницы или большом перерыве поток становится недоступен и заново базелайнится без сигнала. Импульс означает только, что свежая строка была замечена; задержка возможна из-за polling, нет гарантии активного звонка или его окончания. Имена от провайдера (`custom_name`, затем `string_view`, затем `address`) сохраняются Home Assistant и могут содержать адресные персональные данные. SIP-аккаунты и SIP-провижининг интеграция не создаёт.

### Распознавание кодовой фразы (необязательно)

**Ограничения обработки:** после завершения фрагмента есть 10 секунд на ожидание свободного слота, STT и сравнение; просроченный результат не включает сенсор. Сам фрагмент ограничен 8 секундами. Между стартами STT-запросов одного домофона — не менее 5 секунд. Слишком частые фрагменты отбрасываются без накопления очереди; получение аудио продолжается. Эти границы фиксированы в текущей бета-версии, а не являются настройками VAD.

**Отключение не удаляет настройки:** endpoint, токен и защищённые наборы фраз сохраняются для повторного включения. Для их удаления предусмотрен отдельный пункт **Параметры → Сбросить настройки распознавания** с подтверждением. Очистка фраз последнего домофона останавливает функцию, сохраняя параметры STT; редактирование при отключённой функции не включает её автоматически.

Функция **по умолчанию отключена**. В выключенном состоянии интеграция не запускает дополнительный FFmpeg, VAD или STT и не создаёт дополнительной потоковой нагрузки. Шестерёнка **Параметры** записи Ufanet открывает собственную страницу интеграции внутри Home Assistant. Все параметры сервиса находятся на **одной странице**, без «Далее» и второго шага модели:

1. Укажите полный OpenAI-compatible endpoint `/v1/audio/transcriptions`. Сохранённый API-ключ уже заполнен и скрыт; кнопка с глазом показывает и снова скрывает **его настоящее значение** только администратору. Очистка поля и **Сохранить** удаляют ключ. Если изменён протокол, хост или порт, прежний ключ нельзя использовать без явного подтверждения на странице либо замены/очистки. Простое раскрытие или неизменённое значение не считается новым ключом.
2. При желании нажмите отдельную кнопку **Проверить связь и получить модели**. Модель для проверки не нужна. В поле **Модель распознавания** **обязательно** выберите модель из списка подсказок либо введите точный ID вручную перед сохранением. Первая строка, `default` и `aliases.default` автоматически не выбираются; сохранённая непустая модель остаётся выбранной. ID сохраняется без исправлений: 1–128 ASCII-символов, первый — буква/цифра, далее также допустимы `. _ : / -`; пробелы и `default` отклоняются.
3. Нажмите **Сохранить**, чтобы сохранить параметры независимо от проверки. При первоначальной настройке оставьте распознавание выключенным, затем откройте **Кодовые фразы по домофонам**, выберите конкретный текущий доверенный домофон с камерой и введите до 16 фраз, по одной на строку. Включение доступно после настройки фраз. Endpoint, ключ, модель и фразы можно редактировать при выключенной функции.

Каталог запрашивается только по кнопке **Проверить связь и получить модели**, не при открытии страницы, нажатии **Сохранить**, запуске или фоновой работе. Для суффикса `/audio/transcriptions` используется соседний `/models` с сохранением префикса: `/proxy/v1/audio/transcriptions` → `/proxy/v1/models`. Неизвестный/неоднозначный путь не угадывается: форма объясняет отсутствие каталога и оставляет ручной ввод. Ошибка доступа, сети или некорректный/пустой каталог также не стирает настройки. Список общего совместимого сервера **не подтверждает поддержку транскрипции**; проверяйте назначение модели у провайдера. Для точного origin `https://api.openai.com` список ограничен известными именами моделей транскрипции, без Realtime; ручной ввод остаётся доступен.

Запрос каталога ограничен 10 секундами, 256 КиБ и 256 записями до фильтрации; ID сортируются и устраняются дубликаты. Bearer передаётся только в этом запросе, редиректы и автоматические повторы отключены, TLS проверяется как обычно. Политика HTTP совпадает с STT: loopback допускается; частный IP вне loopback — только с явным разрешением и без Bearer. Публичный HTTP не допускается.

**Обновление с пустой моделью:** прежние настройки и списки читаются без миграции, модель не подставляется, выключенная функция остаётся выключенной. Просмотр/редактирование фраз и сброс доступны без модели или каталога. Для простой паузы только выключите включённое распознавание, не меняя endpoint, ключ и HTTP: это сохраняет прежнюю модель и не выполняет GET. Включение и сохранение параметров сервиса (в том числе при выключенной функции) требуют явной модели на той же странице.

**Просмотр и редактирование:** администратор Home Assistant открывает **Параметры → Кодовые фразы по домофонам → нужный домофон**. В списке и редакторе используется ваше имя устройства в Home Assistant; исходное название служит запасным вариантом. Выбор домофона сразу загружает количество фраз и ранее введённый список **под тем же выпадающим списком**, без промежуточного подтверждения. Измените строки и нажмите **Сохранить**. Очистка видимого списка и **Сохранить** удаляют все фразы только этого домофона — отдельной галочки удаления нет. Пустой список нового домофона можно сохранить без добавления фраз. После сохранения выбор домофона и редактор остаются на странице. Списки других домофонов не меняются. При замене камеры прежний список не переносится на новую привязку.

**Старые списки:** если раньше хранились только хеши, точное распознавание продолжает работать, но исходный текст восстановить невозможно. Форма объясняет, что для просмотра нужен однократный повторный ввод списка. Открытие формы и сохранение **нетронутого** пустого поля оставляют старый список без изменений. Для явного удаления такого невидимого списка введите любой текст, затем очистите поле и нажмите **Сохранить**; пояснение есть в редакторе.

Для каждого настроенного домофона используется отдельный FFmpeg audio-only из уже существующего loopback RTSP relay: PCM mono 16 кГц/s16le → локальный bounded VAD/WAV → внешний STT. Аудио ограничено в памяти и не сохраняется. Транскрипт используется только внутри процесса для **точного совпадения после нормализации**; substring и fuzzy matching отсутствуют. Транскрипты, аудио, исходные фразы и их варианты не попадают в state, attributes, Recorder, diagnostics или обычные логи. В Options сохраняются введённые администратором фразы в читаемом виде вместе с прежним Argon2id-защищённым представлением для сравнения. Это восстанавливаемая конфигурация, доступная через стандартную границу прав администратора Home Assistant, а не сохраняемые транскрипты. Фразы и STT-токен могут попасть в backup Home Assistant: защищайте доступ к конфигурации и резервные копии. Во внешний STT отправляются выделенные VAD фрагменты речи, а не только кодовые фразы; хранение данных самим STT-провайдером определяется его политикой.

Это только наблюдательный сенсор. Он не вызывает `button.press`, не содержит команды открытия и не связан с Frigate или MQTT. Недоступность FFmpeg, VAD, STT, камеры либо изменение привязки делает sensor unavailable/неактивным без физического действия. Одновременно выполняется не более двух дорогих STT/matcher операций на event loop.

### Поддерживаемые устройства и ограничения

Поддерживается только семейство общих домофонов **shared SKUD**, доступных текущей учётной записи. Отдельное семейство устройств, модулей и шлагбаумов не поддерживается, пока его связь между инвентарём и физической командой не будет независимо проверена.

Интеграция не добавляет MQTT, Frigate, распознавание лиц, автоматическое голосовое управление дверью или произвольную обработку видеопотока. Необязательный сенсор кодовой фразы обрабатывает только аудио выбранных камер и ничего не открывает. Камеры обнаруживаются по данным каждой добавленной учётной записи и доступны через встроенный loopback-only RTSP relay; он проверяет provider-owned media hosts и скрывает краткоживущие подписанные URL. Функционал автовахтёра не включён до появления отдельного realtime-сигнала вызова и безопасной политики подтверждения.

### Безопасность и приватность

Страница настроек поставляется вместе с интеграцией: без CDN, отдельного приложения или правок Home Assistant frontend. Она доступна только администратору по маршруту `/ufanet-settings?config_entry=<entry_id>`; шестерёнка выбирает нужную запись. Здесь же остаются **Кодовые фразы по домофонам**, **Обновить список домофонов** и **Сбросить настройки распознавания** с прежней проверкой и подтверждениями Options Flow. Обычный Options fallback сохранён; в нём необязательное пустое/пропущенное поле ключа означает сохранение прежнего ключа, а не его удаление. Если настройки изменились в другом окне, сохранение отклоняется: откройте страницу заново. Обычные обновления состояния HA не стирают черновик. Единственная кнопка **Назад** возвращает из фраз, обновления списка или сброса к параметрам сервиса, а оттуда — к интеграции. При несохранённых изменениях смена домофона или **Назад** требуют подтверждения отмены; отказ сохраняет выбор и текст без записи. Если запись фраз уже подтверждена, но редактор не обновился из-за перезагрузки интеграции, сохранённый текст остаётся видимым, а отдельная кнопка **Обновить список фраз** повторяет только чтение. Обычный Options fallback по-прежнему сохраняет старый список при пустом поле и использует явную галочку удаления. Ключ доступен только в памяти приватной страницы, не в localStorage, URL, публичном состоянии, диагностике или логах; [Security Policy](SECURITY.md).

Учётные данные сохраняются локально в стандартной записи конфигурации Home Assistant и используются только для обращения к Ufanet. Они также могут попасть в резервные копии Home Assistant: защищайте backups шифрованием, доступом и политикой хранения так же строго, как пароль. Не публикуйте договор, пароль, токены, provider ID, адрес, данные камеры или сырую диагностику.

Если Home Assistant доступен извне или подключён к голосовому ассистенту, ограничьте доступ к `button.press`: такая интеграция расширяет поверхность физического доступа.

Перед физической командой интеграция выполняет свежую проверку доступного инвентаря. После начала физического запроса повторов нет: при сетевой ошибке или тайм-ауте результат считается неизвестным, чтобы случайно не отправить команду второй раз. Настройка, перезагрузка и read-only обновление не должны открывать дверь.

При обновлении старой записи версии 1.x интеграция локально переводит её в формат v2 без сетевого запроса и без команды открытия. Все прежние физические привязки отбрасываются, запись остаётся в карантине, а работа сущностей блокируется до явного принятия актуальных привязок через **Параметры**. Миграция не создаёт автоматического доверия к прежним или вновь найденным входам.

Управление физическим доступом несёт реальный риск. Проверяйте автоматизации и используйте минимально необходимые права. Уязвимости, связанные с физическим доступом, сообщайте приватно по инструкции в [SECURITY.md](SECURITY.md).

### Установка через HACS

1. Откройте HACS и раздел **Integrations**.
2. Откройте меню пользовательских репозиториев и введите **GitHub URL этого репозитория**.
3. Выберите категорию **Integration** и добавьте репозиторий.
4. В меню репозитория включите **Show beta versions**, затем выберите **Need a different version?** и установите точный тег **v2.1.0b7**. Не устанавливайте бета-версию с ветки по умолчанию.
5. Перезапустите Home Assistant и до настройки или физической проверки убедитесь в HACS, что установленная версия — именно **2.1.0b7**.
6. Откройте **Настройки → Устройства и службы → Добавить интеграцию**, выберите **Ufanet Intercom** и введите договор (логин) и пароль.
7. Необязательно: откройте **Параметры**, настройте внешний STT и кодовые фразы только для нужных домофонов. Не используйте этот сенсор как команду открытия двери.

## English

Ufanet Intercom is an unofficial community integration for Home Assistant. It is not affiliated with, endorsed by, or supported by Ufanet.

> **Version 2.1.0b7 is a beta release.** Install it only through a controlled rollout with a backup, and review every discovered entrance before creating automations. After a network error or unknown outcome, do not retry opening as a diagnostic action. Follow [Controlled rollout and rollback](docs/controlled-rollout-and-rollback.md).

### Behavior and scope

The config flow asks only for the Ufanet contract/login and password and authenticates through the verified mobile-style authentication endpoint. It then performs read-only discovery of every supported **shared SKUD** intercom available to the account. Each intercom becomes one Home Assistant Device with one standard Button entity supporting `button.press`.
Each trusted intercom also gets a neutral **Call detected** binary sensor. It is a short observed-history pulse, not an active-ring state.
Selected trusted intercoms with cameras can also get an optional **Code phrase detected** binary sensor. It turns on for exactly five seconds after an exact match with one configured phrase and **does not open an entrance**.
Each trusted intercom with an available provider camera also gets a **Camera** entity inside the same Device. Camera bindings and the active media server are discovered from that account. Home Assistant receives only a loopback RTSP URL containing an opaque key and consumes it over TCP; short-lived provider tokens stay in process memory and never enter entity state or ffmpeg arguments. No Frigate/go2rtc setup, manual URL, camera ID, or token is required.
The "auto-concierge" feature is intentionally not enabled yet: history polling is not a reliable active-call signal and must not trigger a door-opening command.

Initial setup shows only aggregate discovery counts and requires explicit acknowledgement before creating the entry: the user accepts the physical-actuation warning and trusts all currently discovered bindings. Newly discovered or changed bindings are quarantined and are not automatically trusted. To adopt all pending bindings, open the integration entry's **Options → Refresh intercom list**, review the aggregate new and changed counts, and explicitly confirm adoption.

Provider-supplied display names persist as Home Assistant entity and device names. They may resemble addresses and can remain in registries, history, backups, remote UI, or voice-assistant metadata even if the provider later changes them.

The Button represents a momentary open request, not a physical lock sensor. It has no persistent locked/unlocked state. Before pressing it, the integration refreshes its read-only view of the account. Once the physical request begins, it is never retried, and an ambiguous transport result remains unknown.

Call history is polled by one serialized account-level worker every 3 seconds (up to 28,800 history GETs per account per day). Startup first establishes a silent baseline. Failures, continuity/anchor loss, recovery, or a conservative observation gap make the sensor unavailable and trigger a fresh silent baseline. The pulse only means a fresh history row was observed; polling latency is possible, and it does not guarantee that a call is currently active or indicate when it ended. Provider-derived names (`custom_name`, then `string_view`, then `address`) persist in Home Assistant and may contain address-like personal data. No SIP account provisioning is performed.

### Optional code-phrase recognition

**Processing bounds:** one 10-second budget after a segment completes covers permit waiting, STT and matching; expired results cannot pulse the sensor. Segments themselves are capped at 8 seconds. STT request starts for one intercom are separated by at least 5 seconds. Excess segments are dropped without a backlog while audio capture continues. These are fixed beta limits, not VAD tuning settings.

**Disabling does not erase settings:** endpoint, token and protected phrase sets remain available for re-enabling. Use **Options → Reset recognition settings** with explicit confirmation to erase them. Clearing the final intercom's phrases stops recognition but retains STT settings; editing while disabled does not automatically enable it.

This feature is **disabled by default**. When disabled, no additional FFmpeg, VAD, STT, or streaming work is started. The Ufanet entry's **Options gear** opens the integration-owned settings page inside Home Assistant, with all service fields on **one page**. Enter the full OpenAI-compatible `/v1/audio/transcriptions` endpoint. The saved API key is populated and masked; the eye reveals/hides its actual value for the authenticated administrator. Clearing the field and saving removes the key. A changed scheme, host or port requires a replacement/cleared key or affirmative confirmation before a retained key can be used; revealing/submitting an unchanged key is not replacement consent.

The separate **Проверить связь и получить модели** (Check connection and get models) button requests only metadata, without audio, saving or enabling recognition. Checking needs no model. **Recognition model** requires a choice from the input's dropdown suggestions or an exact manually entered ID before **Сохранить** (Save) saves. No Next or second model step is used. The first item, `default` and `aliases.default` are never automatically selected; a saved nonempty model remains selected. IDs are preserved exactly: 1–128 ASCII characters, starting with a letter/digit, then letters/digits or `. _ : / -`; whitespace and `default` are rejected. First save service settings with recognition off, then use **Code phrases by intercom**, select an exact trusted camera intercom and enter up to 16 phrases, one per line. Enabling needs a configured phrase target; editing can remain paused throughout.

Catalog discovery occurs only on the explicit **Check** button, never on page opening, **Save**, setup or in background workers. Only a known `/audio/transcriptions` suffix maps to sibling `/models`, retaining the base prefix (`/proxy/v1/audio/transcriptions` → `/proxy/v1/models`). Unknown/ambiguous paths cause no request and show a manual-entry explanation. Access/network/invalid/empty-catalog failures also retain all settings and allow manual entry. Generic catalogs **do not verify transcription capability**; check with your provider. Only the exact `https://api.openai.com` origin receives a conservative transcription-name filter, excluding Realtime; manual entry remains available.

The GET has a 10-second deadline, a 256 KiB response cap and at most 256 entries before filtering; IDs are deduplicated and sorted. Bearer is request-local; redirects and automatic retries are disabled, with normal TLS verification. The production STT HTTP policy is unchanged: loopback is allowed; private non-loopback IPs require explicit opt-in and no Bearer; public HTTP is forbidden.

**Upgrading blank-model settings:** old settings and phrase lists remain readable without migration or automatic model assignment/enablement. Phrase editing and reset need neither a model nor discovery. Simply switching enabled recognition off without editing endpoint/key/HTTP pauses it without a GET or model requirement and preserves all settings. Enabling or saving service settings, including while off, requires an explicit model on the same page.

**Viewing and editing:** a Home Assistant administrator opens **Options → Code phrases by intercom → the required intercom**. The selector and editor use your Home Assistant device name, falling back to the original name when needed. Selecting an intercom immediately loads its count and previously entered list **below the same retained dropdown**, without another confirmation step. Edit the lines and press **Save**. Emptying the visible list and saving deletes only that intercom’s phrases, without a separate checkbox. An empty new target can also be saved harmlessly. The selector and editor remain on the page after saving. Other intercoms' lists are unchanged. A replacement camera does not inherit the previous binding's list.

**Legacy lists:** older hash-only lists continue exact matching, but their original text cannot be recovered. The form explains that one-time re-entry is needed to enable viewing. Opening the editor or saving its **untouched** empty field preserves the old list unchanged. To explicitly delete an unseen list, enter any text, clear it, and press **Save**, as explained in the editor.

Each configured intercom uses a separate audio-only FFmpeg input from the existing loopback RTSP relay: 16 kHz mono s16le PCM → local bounded VAD/WAV → external STT. Audio is bounded in memory and not saved. The transcript exists only in process for an **exact normalized match**; substring and fuzzy matching are not used. Audio, transcripts, source phrases, and failed variants never enter state, attributes, Recorder, diagnostics, or ordinary logs. Options store administrator-entered phrases as readable configuration alongside the existing Argon2id-protected matcher representation. This is recoverable configuration behind Home Assistant's standard administrator Options authorization boundary, not persisted STT transcripts. Phrases and the STT token can be included in Home Assistant backups: protect configuration access and backups. VAD-selected speech, not just code phrases, is sent to the external STT; the provider's own retention policy applies there.

This is an observation-only sensor. It never calls `button.press`, contains no opening command, and has no Frigate or MQTT dependency. FFmpeg, VAD, STT, camera, or binding failures remain fail-closed and produce no physical action. At most two expensive STT/matcher operations run process-wide per event loop.

Only the shared SKUD intercom family is supported. The separate device/module/barrier family is unsupported.
The integration does not add MQTT, Frigate, face recognition, automatic voice-controlled opening, or arbitrary video processing. The optional code-phrase sensor processes only audio for selected cameras and does not open anything. Cameras are discovered independently for every added account and are exposed through an embedded loopback-only RTSP relay that validates provider-owned media hosts and redacts short-lived signed URLs. Users cannot provide arbitrary provider IDs, camera URLs, or physical targets. Auto-concierge remains disabled until a separate realtime call signal and a safe confirmation policy exist.

Credentials remain in Home Assistant's local config-entry storage and may therefore be included in Home Assistant backups. Protect backup encryption, access, and retention as carefully as the password itself. Never post credentials, tokens, contract values, provider IDs, addresses, camera data, or raw diagnostics publicly.

When a legacy 1.x entry is upgraded, local v2 migration performs no network request and no physical command. It discards every legacy physical binding, quarantines the entry, and blocks entities until current bindings are explicitly adopted through **Options**. Migration never auto-trusts legacy or newly discovered entrances. A prior `lock.*` registry row is removed as a domain-migration tombstone; the replacement `button.*` row is created with the same opaque unique ID and device relationship, but the old domain-specific entity ID cannot be preserved.

If Home Assistant is remotely accessible or connected to a voice assistant, restrict access to `button.press`; those features expand the physical-access surface. Report physical-access vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

The single **Назад** (Back) button returns from phrases, inventory refresh or reset to service settings, then to the integration. Unsaved device/service drafts require discard confirmation on device change or Back; declining keeps the selection and text without writing. An acknowledged phrase save followed by a failed editor refresh retains the submitted text with a separate read-only retry action, not a misleading failed-save message. The native Options fallback retains its blank-to-keep and explicit-clear-checkbox contract.

### Install with HACS

1. Open **HACS → Integrations → Custom repositories**.
2. Enter **this repository's GitHub URL** and select **Integration**.
3. Add the repository. In its menu, enable **Show beta versions**, select **Need a different version?**, and install the exact **v2.1.0b7** tag. Do not install the beta from the mutable default branch.
4. Restart Home Assistant and verify the installed version in HACS is exactly **2.1.0b7** before configuration or any physical test.
5. Add **Ufanet Intercom** from **Settings → Devices & services** and enter the contract/login and password.
6. Optional: open **Options**, configure an external STT endpoint, and set code phrases only for the required intercoms. Do not use the observation sensor as a door-opening command.

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

`tests/voice_model_options_probe.py` separately exercises real Home Assistant Options forms and selector serialization with synthetic settings/catalog only. Its header contains the standalone command; run it against both supported HA versions. It covers the populated pause form, required blank-model selection, retained/custom IDs, manual fallback and credential edits while off. It performs no network or audio work.

### Local synthetic chain probe

`tests/voice_chain_probe.py` contains the pinned standalone command and requires local FFmpeg plus offline espeak or FFmpeg's libflite voice. It exercises real decoding, microVAD, WAV/HTTP transport, matching, native HA state and cleanup. Its source is a generated file, not production RTSP, and its localhost STT response is scripted. A PASS proves component interaction, not outdoor VAD accuracy, STT recognition quality, or weak-host capacity. It never uses provider credentials, user recordings, remote STT/TTS or physical actions.

Licensed under the [MIT License](LICENSE).
