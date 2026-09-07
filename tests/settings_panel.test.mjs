// Logic-level DOM doubles, not a substitute for the exact HA browser gate.
// Run: node --test tests/settings_panel.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInThisContext } from "node:vm";
import test from "node:test";

class Element {
  constructor(type = "") {
    this.type = type;
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.required = false;
    this.attrs = {};
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.parentElement = {hidden: true};
  }
  set innerHTML(value) { this.html = value; this.children = []; }
  get innerHTML() { return this.html; }
  append(...children) { this.children.push(...children); }
  querySelectorAll(selector) {
    const matches = node => selector.split(",").some(part => {
      part = part.trim();
      if (part.startsWith("[name=")) return node.name === part.slice(7, -2);
      if (part.startsWith("[data-action=")) return node.dataset?.action === part.slice(14, -2);
      if (part.startsWith("#")) return node.id === part.slice(1);
      return node.tagName === part;
    });
    return this.children.filter(child => typeof child === "object").flatMap(child => [
      ...(matches(child) ? [child] : []), ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setAttribute(name, value) { this.attrs[name] = value; }
  reportValidity() { return true; }
}
class Root {
  constructor() { this.map = new Map(); }
  querySelector(selector) {
    for (const element of this.map.values()) {
      const child = element.querySelector(selector);
      if (child) return child;
    }
    if (!this.map.has(selector)) this.map.set(selector, new Element());
    return this.map.get(selector);
  }
  querySelectorAll() { return [...this.map.values()]; }
  set innerHTML(value) { this.html = value; for (const el of this.map.values()) el.replaceChildren(); }
  get innerHTML() { return this.html; }
  replaceChildren() { this.cleared = true; for (const el of this.map.values()) el.replaceChildren(); }
}
globalThis.HTMLElement = class {
  constructor() { this.isConnected = true; }
  attachShadow() { this.shadowRoot = new Root(); }
};
globalThis.window = {
  location: {search: "?config_entry=entry-a"},
  addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
};
globalThis.history = {pushState() {}};
globalThis.CustomEvent = class {};
globalThis.document = {createElement: tag => {const el = new Element(); el.tagName = tag; return el;}};
globalThis.Option = class extends Element {constructor(label, value) {super(); this.textContent = label; this.value = value;}};
let Panel;
globalThis.customElements = {get: () => undefined, define: (_name, value) => {Panel = value;}};
runInThisContext(readFileSync(new URL("../custom_components/ufanet_intercom/frontend/ufanet-settings.js", import.meta.url), "utf8"));
const deferred = () => {let resolve, reject; const promise = new Promise((a, b) => {resolve = a; reject = b;}); return {promise, resolve, reject};};
const saved = () => ({revision: "revision-a", enabled: false, endpoint: "https://stt.invalid/v1/audio/transcriptions", token: "SYNTHETIC-KEY", model: "", allow_insecure_http: false, has_targets: true});
function make(api) {
  window.location.search = "?config_entry=entry-a";
  const panel = new Panel();
  panel._hass = {user: {id: "admin-a", is_admin: true}, callApi: api};
  panel._entry = "entry-a";
  panel._userId = "admin-a";
  panel._saved = saved();
  window.confirm = () => {throw Error("unexpected discard prompt");};
  for (const name of ["enabled", "allow_insecure_http", "confirm_token_origin"]) panel._input(name).type = "checkbox";
  panel._input("token").type = "password";
  panel._shell();
  panel._renderService();
  return panel;
}


test("actual saved key is masked; reveal never turns it into replacement", () => {
  const panel = make(async () => {throw Error("no network");});
  assert.equal(panel._input("token").value, "SYNTHETIC-KEY");
  assert.equal(panel._input("token").type, "password");
  const button = panel.shadowRoot.querySelector('[data-action="reveal"]');
  button.onclick({currentTarget: button});
  assert.equal(panel._input("token").type, "text");
  assert.equal(button.attrs["aria-pressed"], "true");
  assert.equal(panel._draft().token_action, "keep");
  panel._input("endpoint").value = "https://other.invalid/v1/audio/transcriptions";
  panel._updateConstraints();
  assert.equal(panel._input("confirm_token_origin").parentElement.hidden, false);
  assert.equal(panel._draft().confirm_token_origin, false);
  button.onclick({currentTarget: button});
  assert.equal(panel._input("token").type, "password");
});

test("hass state updates preserve unsaved fields and node identity", async () => {
  const panel = make(async () => {throw Error("unexpected GET");});
  const input = panel._input("endpoint");
  input.value = "https://draft.invalid/v1/audio/transcriptions";
  panel.hass = {...panel._hass, states: {"sensor.updated": {state: "on"}}};
  await Promise.resolve();
  assert.equal(panel._input("endpoint"), input);
  assert.equal(panel._input("endpoint").value, "https://draft.invalid/v1/audio/transcriptions");
});

test("check with blank required model is not form submission or save; selection stays exact", async () => {
  const calls = [];
  const panel = make(async (...args) => {calls.push(args); return {models: ["small", "large-v3"], error: null};});
  panel.shadowRoot.querySelector("#service").reportValidity = () => {throw Error("Check must bypass required validation");};
  assert.equal(panel._input("model").required, true);
  await panel._submit(true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1], "ufanet_intercom/settings/entry-a/models");
  assert.equal(calls[0][2].model, "");
  assert.equal(panel._input("model").value, "");
  assert.deepEqual(panel.shadowRoot.querySelector("#models").children.map(option => option.value), ["small", "large-v3"]);
  panel._input("model").value = "Org/Exact:V2";
  await panel._submit(true);
  assert.equal(panel._input("model").value, "Org/Exact:V2");
});

test("failed check keeps selection/catalog and manual save performs POST then readback only", async () => {
  const calls = [];
  const panel = make(async (method, url, body) => {
    calls.push({method, url, body});
    if (url.endsWith("/models")) return {models: [], error: "models_unavailable"};
    return {...saved(), model: "manual", revision: "revision-b"};
  });
  panel._input("model").value = "manual";
  const option = new Option("old", "old");
  panel.shadowRoot.querySelector("#models").append(option);
  await panel._submit(true);
  assert.equal(panel._input("model").value, "manual");
  assert.equal(panel.shadowRoot.querySelector("#models").children[0], option);
  await panel._submit(false);
  assert.deepEqual(calls.map(call => call.method), ["POST", "POST", "GET"]);
  assert.equal(calls[1].url, "ufanet_intercom/settings/entry-a");
  assert.equal(panel._saved.revision, "revision-b");
});

test("blank legacy pure pause removes browser model requirement but edited pause requires it", () => {
  const panel = make(async () => {});
  panel._saved.enabled = true;
  panel._updateConstraints();
  assert.equal(panel._input("model").required, false);
  panel._input("endpoint").value += "/changed";
  panel._updateConstraints();
  assert.equal(panel._input("model").required, true);
});

test("double check/save is gated; changed draft cannot receive stale catalog", async () => {
  const pending = deferred(); let calls = 0;
  const panel = make(async () => {calls++; return pending.promise;});
  const check = panel._submit(true);
  await panel._submit(true); await panel._submit(false);
  assert.equal(calls, 1);
  panel._input("endpoint").value = "https://new.invalid/v1/audio/transcriptions";
  panel.shadowRoot.querySelector("#service").listeners.input({target: {name: "endpoint"}});
  pending.resolve({models: ["stale"], error: null});
  await check;
  assert.equal(panel.shadowRoot.querySelector("#models").children.length, 0);
  assert.equal(panel._busy, false);
});

test("entry switch clears old private fields and discards late check result", async () => {
  const pending = deferred(); const next = {...saved(), token: "SYNTHETIC-SECOND"};
  const panel = make(async method => method === "POST" ? pending.promise : next);
  const token = panel._input("token");
  const check = panel._submit(true);
  window.location.search = "?config_entry=entry-b";
  const switching = panel._syncEntry();
  assert.equal(token.value, "");
  await switching;
  pending.resolve({models: ["old-entry"], error: null});
  await check;
  assert.equal(panel._entry, "entry-b");
  assert.equal(panel._input("token").value, "SYNTHETIC-SECOND");
  assert.equal(panel.shadowRoot.querySelector("#models").children.length, 0);
});

test("unmount clears private input and saved references; pending result cannot render", async () => {
  const pending = deferred();
  const panel = make(async () => pending.promise);
  const input = panel._input("token");
  const check = panel._submit(true);
  panel.isConnected = false; panel.disconnectedCallback();
  assert.equal(input.value, ""); assert.equal(panel._saved, null);
  pending.resolve({models: ["stale"], error: null});
  await check;
  assert.equal(panel._saved, null);
  assert.equal(panel.shadowRoot.cleared, true);
});

test("owned Options flow is deleted on Back/unmount, no other flow IDs", async () => {
  const calls = [];
  const panel = make(async (...args) => {calls.push(args); return {};});
  panel._ownedFlow = "owned-flow";
  panel._back(); panel.disconnectedCallback();
  await Promise.resolve();
  assert.deepEqual(calls, [["DELETE", "config/config_entries/options/flow/owned-flow"]]);
});

test("Options start gap: leaving deletes late created flow without taking next step", async () => {
  const pending = deferred(); const calls = [];
  const panel = make(async (...args) => {calls.push(args); return args[0] === "DELETE" ? {} : pending.promise;});
  const start = panel._startOptions("voice_device");
  panel.disconnectedCallback();
  pending.resolve({flow_id: "late-flow"});
  await start;
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1], ["DELETE", "config/config_entries/options/flow/late-flow"]);
  assert.equal(panel._ownedFlow, null);
});

test("failed second Options POST deletes first flow, preserves fixed error", async () => {
  const calls = [];
  const panel = make(async (...args) => {
    calls.push(args);
    if (calls.length === 1) return {flow_id: "failed-flow"};
    if (args[0] === "POST") throw Error("private exception must not surface");
    return {};
  });
  await panel._startOptions("voice_reset");
  assert.deepEqual(calls[2], ["DELETE", "config/config_entries/options/flow/failed-flow"]);
  assert.equal(panel._ownedFlow, null);
  assert.equal(panel.shadowRoot.querySelector("#error").textContent.includes("private"), false);
});

test("failed Options form POST deletes its flow and returns to usable service settings", async () => {
  const calls = [];
  const panel = make(async (...args) => {
    calls.push(args);
    if (args[0] === "DELETE") return {};
    if (calls.length === 1) return {flow_id: "submitted-flow"};
    if (calls.length === 2) return {type: "form", flow_id: "submitted-flow", step_id: "voice_reset", data_schema: [{name: "acknowledge"}]};
    throw Error("private failed form request");
  });
  let serviceRendered = false;
  const renderService = panel._renderService.bind(panel);
  panel._renderService = () => {serviceRendered = true; renderService();};
  await panel._startOptions("voice_reset");
  const form = panel.shadowRoot.querySelector("#options").children.find(child => typeof child.onsubmit === "function");
  form.querySelector('[name="acknowledge"]').checked = true;
  await form.onsubmit({preventDefault() {}});
  assert.deepEqual(calls.filter(call => call[0] === "DELETE"), [["DELETE", "config/config_entries/options/flow/submitted-flow"]]);
  assert.equal(panel._ownedFlow, null);
  assert.equal(panel._flow, null);
  assert.equal(serviceRendered, true); // Real DOM replacement is covered by browser QA.
  assert.equal(panel._busy, false);
  assert.equal(panel.shadowRoot.querySelector("#error").textContent.includes("private"), false);
});

test("failed cleanup is best effort and cannot clear a different newly owned flow", async () => {
  const panel = make(async () => {throw Error("already gone");});
  panel._ownedFlow = "new-flow";
  panel._abortOptions("old-flow");
  await Promise.resolve();
  assert.equal(panel._ownedFlow, "new-flow");
});

for (const failure of ["network", "reload-abort", "service-unavailable", "missing-selected-target"]) {
  test(`acknowledged Save survives ${failure} readback; retry only reads, then later Save works`, async () => {
    const api = nativeApi(); let fail = false;
    const panel = make(async (method, url, payload) => {
      if (fail && method === "POST" && payload?.next_step_id) {
        fail = false;
        if (failure === "network") throw Error("private readback failure");
        if (failure === "reload-abort") {
          api.flows.delete(url.split("/").at(-1));
          return {type: "abort", reason: "no_voice_targets"};
        }
        if (failure === "missing-selected-target") {
          const picker = await api.api(method, url, payload);
          picker.data_schema[0].options = picker.data_schema[0].options.filter(([key]) => key !== "target-a");
          return picker;
        }
        return {type: "form", flow_id: url.split("/").at(-1), step_id: "voice_service"};
      }
      return api.api(method, url, payload);
    });
    await panel._startOptions("voice_device"); await selectTarget(panel, "target-a");
    const selector = panel._input("target"), savedFlow = panel._ownedFlow;
    const submitted = editPhrases(panel, "Saved despite readback failure"); fail = true;
    await savePhrases(panel);
    assert.equal(panel._input("target"), selector);
    assert.equal(selector.value, "target-a"); assert.equal(selector.disabled, false);
    assert.equal(panel._phrases.target, "target-a");
    assert.equal(panel._phrases.input, submitted);
    assert.equal(panel._input("phrases").value, "Saved despite readback failure");
    assert.equal(panel._input("phrases").disabled, true);
    assert.equal(phraseForm(panel).querySelector('[data-action="options-save"]').disabled, true);
    assert.match(panel.shadowRoot.querySelector("#status").textContent, /сохранены/);
    assert.match(panel.shadowRoot.querySelector("#error").textContent, /обновить/);
    assert.equal(api.flows.size, 0); assert.equal(panel._ownedFlow, null); assert.equal(panel._flow, null);
    assert.equal(api.writes, 1); assert.equal(api.lists["target-a"], submitted.value);
    const calls = api.calls.length;
    await savePhrases(panel); // The retained reference is never a writable candidate.
    assert.equal(api.calls.length, calls);
    const retry = panel.shadowRoot.querySelector("#options").querySelector('[data-action="phrases-reload"]');
    assert.ok(retry);
    if (failure === "missing-selected-target") {
      fail = true; await retry.onclick(); // A remains absent on a refresh-only retry.
      assert.equal(panel._phrases.input, submitted); assert.equal(submitted.disabled, true);
      assert.equal(api.flows.size, 0); assert.equal(api.writes, 1);
      api.lists["target-a"] = ""; // A fresh hardware binding must not inherit the snapshot.
    }
    await retry.onclick();
    assert.equal(api.writes, 1); assert.equal(api.flows.size, 1);
    assert.notEqual(panel._ownedFlow, savedFlow);
    assert.equal(panel._input("phrases").value, failure === "missing-selected-target" ? "" : "Saved despite readback failure");
    assert.equal(panel._input("phrases").disabled, false);
    editPhrases(panel, "Second saved value"); await savePhrases(panel);
    assert.equal(api.lists["target-a"], "Second saved value");
    assert.equal(api.lists["target-b"], "Synthetic phrase B");
    if (failure === "missing-selected-target") {
      fail = true; await savePhrases(panel);
      const writes = api.writes;
      await selectTarget(panel, "target-b");
      assert.equal(panel._input("phrases").value, "Synthetic phrase B");
      assert.equal(panel._input("phrases").disabled, false);
      assert.equal(panel._phrases.editor.querySelector('[data-action="phrases-reload"]'), null);
      assert.equal(api.writes, writes); assert.equal(api.lists["target-a"], "Second saved value");
    }
  });
}

test("ordinary selection of a missing target still clears it and leaves other targets usable", async () => {
  const api = nativeApi(); let missing = false;
  const panel = make(async (method, url, payload) => {
    const result = await api.api(method, url, payload);
    if (missing && payload?.next_step_id === "voice_device") {
      result.data_schema[0].options = result.data_schema[0].options.filter(([key]) => key !== "target-a");
    }
    return result;
  });
  await panel._startOptions("voice_device"); missing = true;
  await selectTarget(panel, "target-a");
  assert.equal(panel._phrases.target, ""); assert.equal(panel._input("target").value, "");
  assert.equal(panel._phrases.input, null); assert.equal(panel._phrases.editor.children.length, 0);
  assert.deepEqual(panel._input("target").children.map(option => option.value), ["", "target-b"]);
  assert.match(panel.shadowRoot.querySelector("#error").textContent, /Выберите его заново/);
  assert.equal(api.flows.size, 0); assert.equal(panel._ownedFlow, null);
  await selectTarget(panel, "target-b");
  assert.equal(panel._input("phrases").value, "Synthetic phrase B");
  assert.equal(api.writes, 0);
});

for (const code of ["invalid_phrases", "stale_target"]) {
  test(`native ${code} validation preserves draft or revokes the stale target`, async () => {
    const api = nativeApi(); let form;
    const panel = make(async (method, url, payload) => {
      if (payload && "phrases" in payload) return {...form, errors: {base: code}};
      const result = await api.api(method, url, payload);
      if (payload?.target) form = result;
      return result;
    });
    await panel._startOptions("voice_device"); await selectTarget(panel, "target-a");
    editPhrases(panel, "Unsaved invalid draft"); await savePhrases(panel);
    assert.equal(api.writes, 0);
    if (code === "invalid_phrases") {
      assert.equal(panel._input("phrases").value, "Unsaved invalid draft");
      window.confirm = () => false;
      await selectTarget(panel, "target-b");
      assert.equal(panel._input("target").value, "target-a");
    } else {
      assert.equal(panel._input("target").value, "");
      assert.equal(panel.shadowRoot.querySelector("#options").querySelector('[name="phrases"]'), null);
      assert.equal(api.flows.size, 0);
      await selectTarget(panel, "target-b");
      assert.equal(panel._input("phrases").value, "Synthetic phrase B");
    }
  });
}

// Stateful native-flow boundary double: reads allocate candidates, only phrase
// submissions persist. Synthetic target IDs/names are never production mappings.
function nativeApi({legacy = false, empty = false} = {}) {
  const calls = [], flows = new Map(), deleted = [];
  const lists = {"target-a": empty ? "" : "Synthetic phrase A", "target-b": "Synthetic phrase B"};
  let counter = 0, writes = 0;
  const api = async (method, url, payload) => {
    calls.push({method, url, payload});
    if (method === "GET") return saved();
    if (url === "config/config_entries/options/flow") {
      const flow_id = `flow-${++counter}`; flows.set(flow_id, {}); return {type: "menu", flow_id};
    }
    const flow_id = url.split("/").at(-1);
    if (method === "DELETE") { deleted.push(flow_id); flows.delete(flow_id); return {}; }
    const state = flows.get(flow_id); assert.ok(state, "POST must use an owned live flow");
    if (payload.next_step_id) {
      state.step = payload.next_step_id;
      return {type: "form", flow_id, step_id: state.step, data_schema: state.step === "voice_device" ? [
        {name: "target", options: [["target-a", "Current HA name A"], ["target-b", "Current HA name B"]]},
      ] : [{name: "acknowledge"}]};
    }
    if (payload.target) {
      assert.equal(state.step, "voice_device"); state.target = payload.target; state.step = "voice_phrases";
      const hidden = legacy && state.target === "target-a";
      return {type: "form", flow_id, step_id: hidden ? "voice_phrases_legacy" : "voice_phrases",
        description_placeholders: {target_name: `Current HA name ${state.target.at(-1).toUpperCase()}`, phrase_count: lists[state.target] ? "1" : "0"},
        data_schema: [{name: "phrases", default: hidden ? "" : lists[state.target]}, {name: "clear_phrases"}]};
    }
    assert.equal(state.step, "voice_phrases");
    writes++;
    if (payload.clear_phrases) { lists[state.target] = ""; legacy = false; }
    else if (payload.phrases.trim()) { lists[state.target] = payload.phrases; legacy = false; }
    flows.delete(flow_id);
    return {type: "create_entry"};
  };
  return {api, calls, flows, deleted, lists, get writes() {return writes;}};
}
const selectTarget = async (panel, value) => {
  const selector = panel._input("target"); selector.value = value;
  await selector.onchange({target: selector});
};
const phraseForm = panel => panel.shadowRoot.querySelector("#options").querySelector("#phrases");
const savePhrases = panel => phraseForm(panel).onsubmit({preventDefault() {}});
const editPhrases = (panel, value) => {
  const input = panel._input("phrases"); input.value = value; input.oninput({target: input}); return input;
};

test("inline dropdown autoloads without intermediate submit or persistence and stays after Save", async () => {
  const api = nativeApi(), panel = make(api.api);
  await panel._startOptions("voice_device");
  const selector = panel._input("target");
  assert.equal(panel.shadowRoot.querySelector("#options").querySelector('[data-action="options-save"]'), null);
  await selectTarget(panel, "target-a");
  assert.equal(panel._input("target"), selector);
  assert.equal(panel._input("phrases").value, api.lists["target-a"]);
  assert.equal(api.writes, 0);
  assert.equal(panel.shadowRoot.querySelector("#options").querySelector('[name="clear_phrases"]'), null);
  editPhrases(panel, "Edited A");
  await savePhrases(panel);
  assert.equal(api.writes, 1);
  assert.equal(panel._input("target"), selector);
  assert.equal(selector.value, "target-a");
  assert.equal(panel._input("phrases").value, "Edited A");
  assert.equal(api.flows.size, 1); // Fresh readback candidate, cleaned on Back.
  assert.match(panel.shadowRoot.querySelector("#status").textContent, /сохранены/);
  await selectTarget(panel, "target-b");
  assert.equal(panel._input("phrases").value, "Synthetic phrase B");
  assert.equal(api.lists["target-a"], "Edited A");
  assert.equal(api.writes, 1);
});

for (const kind of ["readable", "fresh", "legacy", "legacy-touched"]) {
  test(`empty phrase Save maps explicit clear safely: ${kind}`, async () => {
    const api = nativeApi({legacy: kind.startsWith("legacy"), empty: kind === "fresh"}), panel = make(api.api);
    await panel._startOptions("voice_device"); await selectTarget(panel, "target-a");
    if (kind === "readable") editPhrases(panel, " \n ");
    if (kind === "legacy-touched") {editPhrases(panel, "temporary"); editPhrases(panel, "");}
    await savePhrases(panel);
    const payload = api.calls.find(call => call.payload && "phrases" in call.payload).payload;
    assert.equal(payload.clear_phrases, kind !== "legacy");
    assert.equal(api.lists["target-a"], kind === "legacy" ? "Synthetic phrase A" : "");
    assert.equal(api.lists["target-b"], "Synthetic phrase B");
  });
}

test("dirty device change cancel preserves selector/text/flow; accept discards without save", async () => {
  const api = nativeApi(), panel = make(api.api);
  await panel._startOptions("voice_device"); await selectTarget(panel, "target-a");
  const text = editPhrases(panel, "Private draft"), flow = panel._ownedFlow, callCount = api.calls.length;
  window.confirm = () => false;
  await selectTarget(panel, "target-b");
  assert.equal(panel._input("target").value, "target-a"); assert.equal(panel._input("phrases"), text);
  assert.equal(text.value, "Private draft"); assert.equal(panel._ownedFlow, flow); assert.equal(api.calls.length, callCount);
  window.confirm = () => true;
  await selectTarget(panel, "target-b");
  assert.equal(panel._input("phrases").value, "Synthetic phrase B");
  assert.equal(api.flows.has(flow), false); assert.equal(api.writes, 0);
});

for (const step of ["voice_device", "bindings", "voice_reset"]) {
  test(`one contextual Back: ${step} -> service -> integration`, async () => {
    const api = nativeApi(), panel = make(api.api); let exits = 0;
    history.pushState = () => {exits++;};
    await panel._startOptions(step);
    assert.equal(panel.shadowRoot.querySelector("#options").querySelector('[data-action="service"]'), null);
    await panel._back();
    assert.equal(exits, 0); assert.equal(panel._flow, null); assert.equal(api.flows.size, 0);
    await panel._back(); assert.equal(exits, 1);
  });
}

test("dirty phrase/service Back decline is mutation-free; acceptance returns one level", async () => {
  const api = nativeApi(), panel = make(api.api); let exits = 0;
  history.pushState = () => {exits++;};
  await panel._startOptions("voice_device"); await selectTarget(panel, "target-a");
  editPhrases(panel, "Private draft"); const calls = api.calls.length;
  window.confirm = () => false; await panel._back();
  assert.equal(panel._input("phrases").value, "Private draft"); assert.equal(api.calls.length, calls);
  window.confirm = () => true; await panel._back(); assert.equal(exits, 0);
  panel._input("model").value = "draft-model";
  window.confirm = () => false; await panel._back(); assert.equal(exits, 0);
  assert.equal(panel._input("model").value, "draft-model");
  window.confirm = () => true; await panel._back(); assert.equal(exits, 1);
});

test("all custom saving labels say Save; single Back has no redundant destination", async () => {
  const source = readFileSync(new URL("../custom_components/ufanet_intercom/frontend/ufanet-settings.js", import.meta.url), "utf8");
  assert.equal(source.includes("Готово"), false);
  assert.equal(source.includes("Назад к интеграции"), false);
  assert.equal(source.includes("К настройкам распознавания"), false);
  assert.equal((source.match(/data-action="back"/g) || []).length, 2); // markup + binding
  const panel = make(nativeApi().api);
  await panel._startOptions("voice_device"); await selectTarget(panel, "target-a");
  assert.equal(phraseForm(panel).querySelector('[data-action="options-save"]').textContent, "Сохранить");
});

for (const gap of ["start", "menu", "target", "save", "readback-start", "readback-target"]) {
  test(`Back in ${gap} await cleans every owned candidate and rejects late reply`, async () => {
    const api = nativeApi(), pending = deferred(); let hold = false, waiting = false;
    const panel = make(async (method, url, payload) => {
      const result = await api.api(method, url, payload);
      const matches = gap.includes("start") ? url.endsWith("/flow") : gap === "menu" ? payload?.next_step_id : gap.includes("target") ? payload?.target : payload && "phrases" in payload;
      if (hold && method === "POST" && matches) {waiting = true; await pending.promise;}
      return result;
    });
    let operation;
    if (["start", "menu"].includes(gap)) {hold = true; operation = panel._startOptions("voice_device");}
    else {
      await panel._startOptions("voice_device");
      if (gap === "target") {hold = true; operation = selectTarget(panel, "target-a");}
      else {await selectTarget(panel, "target-a"); hold = true; operation = savePhrases(panel);}
    }
    for (let i = 0; i < 30 && !waiting; i++) await Promise.resolve();
    assert.equal(waiting, true);
    await panel._back(); pending.resolve(); await operation;
    assert.equal(api.flows.size, 0); assert.equal(panel._ownedFlow, null); assert.equal(panel._flow, null);
  });
}

for (const gap of ["start", "menu", "target"]) {
test(`switch during pending A ${gap} cannot put A data under B, or abort B flow`, async () => {
  const api = nativeApi(), pending = deferred(); let waiting = false, hold = false;
  const panel = make(async (method, url, payload) => {
    const result = await api.api(method, url, payload);
    const matches = gap === "start" ? url.endsWith("/flow") : gap === "menu" ? payload?.next_step_id : payload?.target === "target-a";
    if (hold && !waiting && method === "POST" && matches) {waiting = true; await pending.promise;}
    return result;
  });
  await panel._startOptions("voice_device");
  hold = true;
  const a = selectTarget(panel, "target-a");
  for (let i = 0; i < 30 && !waiting; i++) await Promise.resolve();
  assert.equal(waiting, true);
  await selectTarget(panel, "target-b"); const bFlow = panel._ownedFlow;
  pending.resolve(); await a;
  assert.equal(panel._input("target").value, "target-b"); assert.equal(panel._input("phrases").value, "Synthetic phrase B");
  assert.equal(panel._ownedFlow, bFlow); assert.equal(api.flows.size, 1);
  editPhrases(panel, "B-only edit"); await savePhrases(panel);
  assert.equal(api.lists["target-a"], "Synthetic phrase A"); assert.equal(api.lists["target-b"], "B-only edit");
});

}

for (const action of ["Back", "switch"]) {
  test(`pending owned DELETE before target load is safely superseded by ${action}`, async () => {
    const api = nativeApi(), pending = deferred(); let hold = false, waiting = false;
    const panel = make(async (method, url, payload) => {
      const result = await api.api(method, url, payload);
      if (hold && method === "DELETE" && !waiting) {waiting = true; await pending.promise;}
      return result;
    });
    await panel._startOptions("voice_device"); hold = true;
    const selecting = selectTarget(panel, "target-a");
    for (let i = 0; i < 20 && !waiting; i++) await Promise.resolve();
    assert.equal(waiting, true);
    if (action === "Back") await panel._back();
    else await selectTarget(panel, "target-b");
    pending.resolve(); await selecting;
    assert.equal(api.calls.some(call => call.payload?.target === "target-a"), false);
    assert.equal(api.flows.size, action === "Back" ? 0 : 1);
    if (action === "switch") assert.equal(panel._input("phrases").value, "Synthetic phrase B");
  });
}

for (const failure of ["menu", "target", "save"]) {
  test(`failed ${failure} POST cleans owned flow; later new selection and Save work`, async () => {
    const api = nativeApi(); let fail = false;
    const panel = make(async (method, url, payload) => {
      const matches = failure === "menu" ? payload?.next_step_id : failure === "save" ? payload && "phrases" in payload : payload?.target;
      if (fail && method === "POST" && matches) {fail = false; throw Error("private failure");}

      return api.api(method, url, payload);
    });
    if (failure === "menu") fail = true;
    await panel._startOptions("voice_device");
    if (failure !== "menu") {
      if (failure === "target") fail = true;
      await selectTarget(panel, "target-a");
      if (failure === "save") {fail = true; await savePhrases(panel);}
    }
    assert.equal(api.flows.size, 0); assert.equal(panel._ownedFlow, null); assert.equal(panel._busy, false);
    assert.equal(panel.shadowRoot.querySelector("#error").textContent.includes("private"), false);
    await panel._startOptions("voice_device"); await selectTarget(panel, "target-b");

    editPhrases(panel, "Later B edit"); await savePhrases(panel);
    assert.equal(api.lists["target-b"], "Later B edit");
  });
}
