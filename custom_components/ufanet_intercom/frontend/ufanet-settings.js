/* Bundled HA administrator panel. No external modules or persistent browser state. */
const ERRORS = {
  unauthorized: "Войдите в Home Assistant.",
  forbidden: "Настройки доступны только администратору.",
  not_found: "Запись Ufanet не найдена. Откройте настройки нужной интеграции.",
  invalid_request: "Не удалось прочитать настройки. Откройте страницу заново.",
  invalid_stt: "Проверьте полный адрес сервиса, ключ и разрешение HTTP.",
  invalid_model: "Выберите модель или введите её точный ID.",
  token_origin_changed: "Сервер изменён: замените или очистите ключ либо явно разрешите использовать сохранённый ключ на новом сервере.",
  no_voice_targets: "Сначала сохраните настройки с выключенным распознаванием и настройте кодовые фразы по домофонам. Затем включите распознавание.",
  stale_revision: "Настройки изменились в другом окне. Черновик не сохранён. Откройте страницу заново и повторите изменения.",
  busy: "Дождитесь завершения текущей проверки или сохранения.",
  models_unavailable: "Не удалось получить модели. Проверьте адрес и ключ или введите модель вручную.",
  models_unsupported: "Этот адрес не поддерживает получение списка. Введите модель вручную.",
  models_empty: "В каталоге нет подходящих моделей. Введите точный ID вручную.",
  unknown: "Действие не выполнено. Повторите попытку.",
  no_voice_targets_flow: "Нет доступных доверенных домофонов с камерой.",
  voice_service_required: "Сначала сохраните параметры сервиса на основной странице.",
  invalid_phrases: "Проверьте список: до 16 непустых фраз, по одной на строку.",
  phrases_reload_failed: "Не удалось обновить редактор после сохранения. Обновите список фраз или выберите другой домофон.",
  stale_target: "Домофон изменился или недоступен. Выберите его заново.",
  too_many_targets: "Достигнут предел настроенных домофонов.",
  cannot_connect: "Не удалось обновить список домофонов.",
  invalid_auth: "Требуется повторная авторизация Ufanet.",
  invalid_targets: "Не удалось прочитать список домофонов.",
  no_pending_targets: "Новых или изменённых домофонов нет.",
  adoption_successful: "Список домофонов обновлён.",
};
const MENUS = {
  voice_device: "Кодовые фразы по домофонам",
  bindings: "Обновить список домофонов",
  voice_reset: "Сбросить настройки распознавания",
};
const STYLES = `
  :host {display:block; height:100%; overflow:auto; color:var(--primary-text-color); background:var(--primary-background-color); font:16px var(--paper-font-body1_-_font-family, sans-serif)}
  main {max-width:760px; margin:auto; padding:20px; box-sizing:border-box}
  h1 {font-size:24px} h2 {font-size:20px} p, summary {line-height:1.5}
  label {display:block; margin:18px 0} label span {display:block; margin-bottom:8px}
  input:not([type=checkbox]), textarea, select {box-sizing:border-box; width:100%; padding:12px; font:inherit; color:var(--primary-text-color); background:var(--card-background-color); border:1px solid var(--divider-color, #888); border-radius:6px}
  input[type=checkbox] {width:20px; height:20px; vertical-align:middle; margin-right:8px}
  textarea {min-height:160px} button {font:inherit; padding:10px 16px; border:1px solid var(--divider-color, #888); border-radius:6px; background:var(--card-background-color); color:var(--primary-color, #03a9f4); cursor:pointer}
  button:disabled {opacity:.5; cursor:wait} :focus-visible {outline:2px solid var(--primary-color, #03a9f4); outline-offset:2px}
  .row {display:flex; gap:8px; align-items:center; flex-wrap:wrap} .key {flex-wrap:nowrap} .key input {flex:1; min-width:0}
  .actions {margin:24px 0} .primary {background:var(--primary-color, #03a9f4); color:var(--text-primary-color, white)}
  .muted {color:var(--secondary-text-color)} [role=status], [role=alert] {white-space:pre-line; line-height:1.5; margin:16px 0}
  [role=alert] {color:var(--error-color, #db4437)} nav {display:grid; gap:12px; margin-top:28px} nav button {text-align:left}
  [hidden] {display:none!important} .confirm {border-left:3px solid var(--warning-color, #ffa600); padding-left:12px}
`;

class UfanetSettingsPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._epoch = 0;
    this._draftVersion = 0;
    this._entry = null;
    this._saved = null;
    this._busy = false;
    this._flow = null;
    this._ownedFlow = null;
    this._view = "service";
    this._phrases = null;
    this._navigation = () => this._syncEntry();
  }
  set hass(value) {
    this._hass = value;
    this._syncEntry(); // Routine state updates must not reconstruct this form.
  }
  set route(value) { this._route = value; this._syncEntry(); }
  set panel(value) { this._panel = value; this._syncEntry(); }
  connectedCallback() {
    window.addEventListener("popstate", this._navigation);
    window.addEventListener("location-changed", this._navigation);
    this._syncEntry();
  }
  disconnectedCallback() {
    window.removeEventListener("popstate", this._navigation);
    window.removeEventListener("location-changed", this._navigation);
    this._clear();
  }
  _clear() {
    this._abortOptions(this._ownedFlow);
    this._epoch++;
    this._entry = null;
    this._saved = null;
    this._flow = null;
    this._view = "service";
    this._phrases = null;
    this._busy = false;
    this.shadowRoot.querySelectorAll("input, textarea").forEach(el => {el.value = "";});
    this.shadowRoot.replaceChildren();
  }
  _abortOptions(flowId) {
    if (!flowId) return;
    if (this._ownedFlow === flowId) this._ownedFlow = null;
    // Only this panel's own flow, including a late start after navigation.
    // Completion/removal may already have deleted it; cleanup is best effort.
    return this._hass.callApi("DELETE", `config/config_entries/options/flow/${encodeURIComponent(flowId)}`).catch(() => {});
  }
  _shell() {
    this.shadowRoot.innerHTML = `<style>${STYLES}</style><main>
      <button type="button" data-action="back">Назад</button>
      <h1>Ufanet · Настройки распознавания речи</h1>
      <div role="alert" id="error"></div><div role="status" id="status" aria-live="polite"></div>
      <section id="content"></section></main>`;
    this.shadowRoot.querySelector('[data-action="back"]').onclick = () => this._back();
  }
  _back() {
    if (!this._discardDraft()) return;
    const nested = this._view !== "service";
    this._clear();
    if (nested) return this._syncEntry();
    history.pushState(null, "", "/config/integrations/integration/ufanet_intercom");
    window.dispatchEvent(new CustomEvent("location-changed"));
  }
  _message(code, status = "") {
    this.shadowRoot.querySelector("#error").textContent = code ? (ERRORS[code] || ERRORS.unknown) : "";
    this.shadowRoot.querySelector("#status").textContent = status;
  }
  _active(epoch) { return this.isConnected && epoch === this._epoch; }
  async _syncEntry() {
    if (!this.isConnected || !this._hass) return;
    const entry = new URLSearchParams(window.location.search).get("config_entry");
    const user = this._hass.user;
    if (!user?.is_admin || !entry) {
      this._clear(); this._shell();
      this._message(user?.is_admin ? "not_found" : "forbidden");
      return;
    }
    if (entry === this._entry && user.id === this._userId) return;
    this._clear();
    this._entry = entry;
    this._userId = user.id;
    this._shell();
    const epoch = this._epoch;
    this._message(null, "Загрузка настроек…");
    try {
      const saved = await this._hass.callApi("GET", this._url());
      if (!this._active(epoch)) return;
      this._saved = saved;
      this._renderService();
      this._message(null);
    } catch (error) {
      if (this._active(epoch)) this._message(error?.body?.error || "unknown");
    }
  }
  _url() { return `ufanet_intercom/settings/${encodeURIComponent(this._entry)}`; }
  _input(name) { return this.shadowRoot.querySelector(`[name="${name}"]`); }
  _renderService() {
    this._flow = null;
    this._view = "service";
    this._phrases = null;
    this.shadowRoot.querySelector("#content").innerHTML = `
      <form id="service" autocomplete="off">
        <label><input type="checkbox" name="enabled">Включить распознавание</label>
        <label><span>Полный адрес сервиса распознавания</span><input name="endpoint" type="url" required maxlength="2048" placeholder="https://…/v1/audio/transcriptions" autocomplete="off" spellcheck="false"></label>
        <label><span>API-ключ</span><div class="row key"><input name="token" type="password" maxlength="2048" autocomplete="new-password" spellcheck="false"><button type="button" data-action="reveal" aria-label="Показать API-ключ" aria-pressed="false"><svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5C21.27 7.61 17 4.5 12 4.5m0 12.5a5 5 0 1 1 0-10 5 5 0 0 1 0 10m0-8a3 3 0 1 0 0 6 3 3 0 0 0 0-6"/></svg></button></div></label>
        <p class="muted">Сохранённый ключ скрыт. Глаз показывает его значение. Чтобы удалить ключ, очистите поле и нажмите «Сохранить».</p>
        <label class="confirm" hidden><input type="checkbox" name="confirm_token_origin">Разрешаю использовать сохранённый ключ на новом сервере</label>
        <label><span>Модель распознавания (обязательно)</span><input name="model" list="models" required maxlength="128" autocomplete="off" spellcheck="false" placeholder="Выберите из списка или введите точный ID"><datalist id="models"></datalist></label>
        <details><summary>Дополнительные настройки HTTP</summary><label><input type="checkbox" name="allow_insecure_http">Разрешить HTTP для частного IP без API-ключа</label></details>
        <div class="row actions"><button type="button" data-action="check">Проверить связь и получить модели</button><button type="submit" data-action="save" class="primary">Сохранить</button></div>
        <p class="muted">Проверка получает только список моделей. Она не сохраняет настройки, не включает распознавание и не отправляет аудио. Доступность каталога не подтверждает распознавание речи или наличие средств.</p>
      </form><nav aria-label="Другие настройки Ufanet"></nav><section id="options" hidden></section>`;
    for (const name of ["enabled", "endpoint", "token", "model", "allow_insecure_http"]) {
      const el = this._input(name);
      if (el.type === "checkbox") el.checked = this._saved[name];
      else el.value = this._saved[name]; // Actual key only in this private input's value, never markup.
    }
    const form = this.shadowRoot.querySelector("#service");
    form.addEventListener("input", event => {
      this._draftVersion++;
      if (["endpoint", "token", "allow_insecure_http"].includes(event.target.name)) {
        this._input("confirm_token_origin").checked = false;
        this.shadowRoot.querySelector("#models").replaceChildren();
        this._message(null);
      }
      this._updateConstraints();
    });
    form.onsubmit = event => {event.preventDefault(); this._submit(false);};
    this.shadowRoot.querySelector('[data-action="check"]').onclick = () => this._submit(true);
    this.shadowRoot.querySelector('[data-action="reveal"]').onclick = event => {
      const input = this._input("token");
      const revealed = input.type === "password";
      input.type = revealed ? "text" : "password";
      event.currentTarget.setAttribute("aria-pressed", String(revealed));
      event.currentTarget.setAttribute("aria-label", revealed ? "Скрыть API-ключ" : "Показать API-ключ");
    };
    const nav = this.shadowRoot.querySelector("nav");
    for (const [step, title] of Object.entries(MENUS)) {
      const button = document.createElement("button");
      button.type = "button"; button.dataset.options = step; button.textContent = title;
      button.onclick = () => this._startOptions(step);
      nav.append(button);
    }
    this._updateConstraints();
  }
  _draft() {
    return {
      revision: this._saved.revision,
      enabled: this._input("enabled").checked,
      endpoint: this._input("endpoint").value,
      token: this._input("token").value,
      token_action: this._input("token").value === this._saved.token ? "keep" : "replace",
      confirm_token_origin: this._input("confirm_token_origin").checked,
      model: this._input("model").value,
      allow_insecure_http: this._input("allow_insecure_http").checked,
    };
  }
  _updateConstraints() {
    const draft = this._draft();
    const pause = this._saved.enabled && !draft.enabled && ["endpoint", "token", "model", "allow_insecure_http"].every(key => draft[key] === this._saved[key]);
    this._input("model").required = !pause;
    let changed = false;
    try { changed = new URL(draft.endpoint).origin !== new URL(this._saved.endpoint).origin; } catch (_) { /* Backend validates incomplete URLs. */ }
    this._input("confirm_token_origin").parentElement.hidden = !(changed && draft.token && draft.token_action === "keep");
  }
  _setBusy(value) {
    this._busy = value;
    this.shadowRoot.querySelectorAll("#content input, #content textarea, #content select, #content button").forEach(el => {el.disabled = value;});
    if (this._phrases?.input) {
      const p = this._phrases;
      p.input.disabled = value || !!p.needsReload;
      p.editor.querySelector('[data-action="options-save"]').disabled = value || !!p.needsReload;
    }
  }
  async _submit(checking) {
    if (this._busy || this._view !== "service" || !this._saved) return;
    const draft = this._draft();
    // Check is deliberately a non-submit button: required model never blocks it.
    if (!checking && !this.shadowRoot.querySelector("#service").reportValidity()) return;
    const epoch = this._epoch;
    const version = this._draftVersion;
    this._setBusy(true);
    this._message(null, checking ? "Получение списка моделей…" : "Сохранение…");
    try {
      const result = await this._hass.callApi("POST", this._url() + (checking ? "/models" : ""), draft);
      if (!this._active(epoch) || version !== this._draftVersion) return;
      if (checking) {
        if (!result.error) {
          const options = result.models.map(id => {const option = document.createElement("option"); option.value = id; return option;});
          this.shadowRoot.querySelector("#models").replaceChildren(...options);
        }
        // Do not select the first catalog row or touch the current model on failure.
        this._message(result.error, result.error ? "" : "Каталог моделей доступен. Выберите модель или введите ID вручную.");
      } else {
        // Verify exact persisted target before claiming success. GET never discovers.
        const saved = await this._hass.callApi("GET", this._url());
        if (!this._active(epoch) || version !== this._draftVersion) return;
        this._saved = saved;
        this._renderService();
        this._message(null, "Настройки сохранены.");
      }
    } catch (error) {
      if (this._active(epoch) && version === this._draftVersion) this._message(error?.body?.error || "unknown");
    } finally {
      if (this._active(epoch)) this._setBusy(false);
    }
  }
  _dirty() {
    if (this._phrases?.input) {
      const p = this._phrases;
      // Touching an initially hidden legacy list changes empty Save semantics.
      return p.input.value !== p.initial || (p.legacy && p.touched);
    }
    if (this._view !== "service" || !this._saved) return false;
    const draft = this._draft();
    return ["enabled", "endpoint", "token", "model", "allow_insecure_http"].some(key => draft[key] !== this._saved[key]);
  }
  _discardDraft() {
    return !this._dirty() || window.confirm("Изменения не сохранены. Отменить их и продолжить?");
  }
  _optionsUrl(flowId) { return `config/config_entries/options/flow/${encodeURIComponent(flowId)}`; }
  async _openOptions(step, epoch) {
    let flowId;
    try {
      const flow = await this._hass.callApi("POST", "config/config_entries/options/flow", {handler: this._entry, show_advanced_options: true});
      flowId = flow.flow_id;
      if (!this._active(epoch)) { await this._abortOptions(flowId); return null; }
      if (!flowId) throw new Error("unknown");
      this._ownedFlow = flowId;
      const result = await this._hass.callApi("POST", this._optionsUrl(flowId), {next_step_id: step});
      if (!this._active(epoch)) { await this._abortOptions(flowId); return null; }
      return result;
    } catch (_) {
      await this._abortOptions(flowId);
      throw new Error("unknown");
    }
  }
  _optionsContainer(title) {
    this.shadowRoot.querySelector("#service").hidden = true;
    this.shadowRoot.querySelector("nav").hidden = true;
    const container = this.shadowRoot.querySelector("#options");
    container.hidden = false;
    container.replaceChildren();
    const heading = document.createElement("h2"); heading.textContent = title;
    container.append(heading);
    return container;
  }
  _optionsFailure(epoch) {
    if (!this._active(epoch)) return;
    this._abortOptions(this._ownedFlow);
    this._renderService();
    this._message("unknown");
  }
  async _startOptions(step) {
    if (this._busy || !this._discardDraft()) return;
    // Service drafts may only be discarded by the explicit confirmation above.
    if (this._dirty()) this._renderService();
    this._view = step;
    const epoch = ++this._epoch;
    this._optionsContainer(MENUS[step]);
    this._setBusy(true);
    this._message(null, "Загрузка…");
    try {
      const result = await this._openOptions(step, epoch);
      if (result && this._active(epoch)) this._renderOptions(result);
    } catch (_) { this._optionsFailure(epoch); }
    finally { if (this._active(epoch)) this._setBusy(false); }
  }
  _renderPicker(result) {
    if (this._phrases) {
      const p = this._phrases;
      p.target = ""; p.input = null; p.editor.replaceChildren();
      this._updateTargets(result);
      this._message(Object.values(result.errors || {})[0]);
      return;
    }
    const container = this._optionsContainer(MENUS.voice_device);
    const description = document.createElement("p");
    description.textContent = "Выберите домофон: его кодовые фразы появятся ниже. Изменения сохраняются только кнопкой «Сохранить».";
    const label = document.createElement("label"); label.append("Домофон");
    const selector = document.createElement("select"); selector.name = "target";
    label.append(selector);
    const editor = document.createElement("section"); editor.id = "phrase-editor";
    container.append(description, label, editor);
    this._phrases = {selector, editor, target: "", input: null};
    this._updateTargets(result);
    selector.onchange = () => this._selectTarget(selector.value);
    this._message(Object.values(result.errors || {})[0]);
  }
  _updateTargets(result, preserveMissing = false) {
    const {selector, target} = this._phrases;
    const options = result.data_schema?.find(field => field.name === "target")?.options || [];
    const available = options.some(option => option[0] === target);
    // Readback keeps the old selection only as a readonly recovery reference.
    if (preserveMissing && !available) return false;
    selector.replaceChildren(new Option("Выберите домофон", ""), ...options.map(option => new Option(option[1], option[0])));
    selector.value = available ? target : "";
    return selector.value === target;
  }
  async _selectTarget(target) {
    const p = this._phrases;
    if (!p) return;
    if (target === p.target) return;
    if (!this._discardDraft()) { p.selector.value = p.target; return; }
    const epoch = ++this._epoch;
    p.target = target;
    p.input = null;
    p.editor.replaceChildren();
    this._flow = null;
    this._setBusy(true);
    // Loading may be superseded by another selection; Save itself disables it.
    p.selector.disabled = false;
    this._message(null, target ? "Загрузка фраз…" : "Выберите домофон.");
    try {
      await this._abortOptions(this._ownedFlow);
      if (!this._active(epoch) || !target) return;
      await this._loadPhrases(epoch);
    } catch (_) { this._optionsFailure(epoch); }
    finally { if (this._active(epoch)) this._setBusy(false); }
  }
  async _loadPhrases(epoch, readback = false) {
    const p = this._phrases;
    const picker = await this._openOptions("voice_device", epoch);
    if (!picker || !this._active(epoch)) return false;
    if (picker.type !== "form" || picker.step_id !== "voice_device") {
      if (readback) throw new Error("unknown");
      this._renderOptions(picker); return false;
    }
    // Refresh names from HA on every fresh native flow; never map by list index.
    if (!this._updateTargets(picker, readback)) {
      if (readback) throw new Error("unknown");
      await this._abortOptions(this._ownedFlow);
      if (this._active(epoch)) {
        p.target = ""; p.input = null; p.editor.replaceChildren();
        this._flow = null; this._message("stale_target");
      }
      return false;
    }
    const flowId = this._ownedFlow;
    try {
      const result = await this._hass.callApi("POST", this._optionsUrl(flowId), {target: p.target});
      if (!this._active(epoch)) { await this._abortOptions(flowId); return false; }
      if (result.type === "form" && ["voice_phrases", "voice_phrases_legacy"].includes(result.step_id)) {
        this._renderPhraseEditor(result); return !Object.keys(result.errors || {}).length;
      }
      if (readback) throw new Error("unknown");
      this._renderOptions(result);
      return false;
    } catch (_) {
      await this._abortOptions(flowId);
      throw new Error("unknown");
    }
  }
  _renderPhraseEditor(result, draft = null) {
    this._flow = result;
    const p = this._phrases;
    p.editor.replaceChildren(); p.input = null; p.needsReload = false;
    const error = Object.values(result.errors || {})[0];
    if (error === "stale_target") {
      // Backend invalidated the binding. Never replay a draft onto new hardware.
      this._abortOptions(this._ownedFlow);
      p.target = ""; p.selector.value = "";
      this._message(error); return;
    }
    const info = result.description_placeholders || {};
    p.legacy = result.step_id === "voice_phrases_legacy";
    const description = document.createElement("p");
    description.textContent = `Домофон: ${info.target_name || "—"}. Фраз: ${info.phrase_count || "0"}. По одной на строку. ` + (p.legacy
      ? "Старый список хранится только в виде хешей. Для просмотра введите его один раз заново. Нетронутое пустое поле сохраняет старый список. Чтобы удалить его, введите любой текст, затем очистите поле и нажмите «Сохранить»."
      : "Чтобы удалить все фразы этого домофона, очистите список и нажмите «Сохранить».");
    const form = document.createElement("form"); form.id = "phrases"; form.autocomplete = "off";
    const label = document.createElement("label"); label.append("Кодовые фразы");
    const input = document.createElement("textarea"); input.name = "phrases"; input.maxLength = 8192;
    input.value = draft?.value ?? result.data_schema?.find(field => field.name === "phrases")?.default ?? "";
    p.initial = draft?.initial ?? input.value;
    p.touched = draft?.touched ?? false;
    p.input = input;
    input.oninput = () => { p.touched = true; };
    label.append(input); form.append(label, this._saveButton());
    form.onsubmit = event => {event.preventDefault(); return this._savePhrases();};
    p.editor.append(description, form);
    this._message(error);
  }
  _saveButton() {
    const button = document.createElement("button");
    button.type = "submit"; button.textContent = "Сохранить"; button.dataset.action = "options-save";
    return button;
  }
  async _savePhrases() {
    const p = this._phrases;
    const flowId = this._ownedFlow;
    if (this._busy || !p?.input || !flowId || this._flow?.flow_id !== flowId) return;
    const draft = {value: p.input.value, initial: p.initial, touched: p.touched};
    const payload = {phrases: draft.value, clear_phrases: !draft.value.trim() && (!p.legacy || p.touched)};
    const epoch = this._epoch;
    this._setBusy(true); this._message(null, "Сохранение…");
    try {
      const result = await this._hass.callApi("POST", this._optionsUrl(flowId), payload);
      if (!this._active(epoch)) { await this._abortOptions(flowId); return; }
      if (result.type === "create_entry") {
        this._ownedFlow = null; this._flow = null;
        p.initial = p.input.value; p.touched = false;
        await this._readBackPhrases(epoch);
      } else if (result.type === "form" && ["voice_phrases", "voice_phrases_legacy"].includes(result.step_id)) {
        this._renderPhraseEditor(result, draft);
      } else this._renderOptions(result);
    } catch (_) {
      await this._abortOptions(flowId);
      this._optionsFailure(epoch);
    } finally { if (this._active(epoch)) this._setBusy(false); }
  }
  async _readBackPhrases(epoch) {
    const p = this._phrases;
    // Keep submitted text visible until readback, but never save to a completed
    // flow or carry that text into a newly adopted device's fresh candidate.
    p.needsReload = true;
    try {
      if (await this._loadPhrases(epoch, true) && this._active(epoch)) this._message(null, "Кодовые фразы сохранены.");
    } catch (_) {
      if (!this._active(epoch)) return;
      await this._abortOptions(this._ownedFlow);
      if (!this._active(epoch)) return;
      this._message("phrases_reload_failed", "Кодовые фразы сохранены.");
      if (!p.editor.querySelector('[data-action="phrases-reload"]')) {
        const retry = document.createElement("button");
        retry.type = "button"; retry.dataset.action = "phrases-reload";
        retry.textContent = "Обновить список фраз";
        retry.onclick = async () => {
          if (this._busy || this._phrases !== p) return;
          const nextEpoch = ++this._epoch;
          this._setBusy(true);
          await this._readBackPhrases(nextEpoch);
          if (this._active(nextEpoch)) this._setBusy(false);
        };
        p.editor.append(retry);
      }
    }
  }
  _renderOptions(result) {
    const terminal = ["create_entry", "abort"].includes(result.type);
    if (terminal) this._ownedFlow = null;
    this._flow = result;
    if (result.type === "form" && result.step_id === "voice_device") {
      this._renderPicker(result); return;
    }
    this._phrases = null;
    const container = this._optionsContainer(MENUS[this._view]);
    const description = document.createElement("p");
    const p = result.description_placeholders || {};
    const descriptions = {
      voice_reset: "Подтвердите удаление адреса STT, API-ключа, модели и всех кодовых фраз. Камеры и другие настройки интеграции останутся.",
      adopt: `Новых домофонов: ${p.new_count || "0"}; изменённых: ${p.changed_count || "0"}. Подтвердите обновление списка домофонов. Это не открывает дверь.`,
    };
    description.textContent = descriptions[result.step_id] || "";
    container.append(description);
    // Only existing native contracts: Python still owns validation and bindings.
    if (result.type === "form" && ["voice_reset", "adopt", "bindings"].includes(result.step_id)) {
      const form = document.createElement("form");
      for (const field of result.data_schema || []) {
        if (field.name !== "acknowledge") continue;
        const label = document.createElement("label");
        const input = document.createElement("input"); input.type = "checkbox";
        input.name = field.name; input.checked = false;
        label.append(input, "Подтверждаю"); form.append(label);
      }
      form.append(this._saveButton());
      form.onsubmit = async event => {
        event.preventDefault(); if (this._busy || this._ownedFlow !== result.flow_id) return;
        const payload = {};
        for (const input of form.querySelectorAll("input")) payload[input.name] = input.checked;
        const epoch = this._epoch;
        this._setBusy(true);
        try {
          const next = await this._hass.callApi("POST", this._optionsUrl(result.flow_id), payload);
          if (!this._active(epoch)) { await this._abortOptions(result.flow_id); return; }
          this._renderOptions(next);
        } catch (_) {
          await this._abortOptions(result.flow_id);
          this._optionsFailure(epoch);
        } finally { if (this._active(epoch)) this._setBusy(false); }
      };
      container.append(form);
      this._message(Object.values(result.errors || {})[0]);
    } else if (result.type === "create_entry") {
      this._message(null, "Настройки сохранены.");
    } else {
      this._abortOptions(this._ownedFlow);
      this._message(result.step_id === "voice_service" ? "voice_service_required" : result.reason === "no_voice_targets" ? "no_voice_targets_flow" : result.reason || "unknown");
    }
  }
}
if (!customElements.get("ufanet-settings-panel")) customElements.define("ufanet-settings-panel", UfanetSettingsPanel);
