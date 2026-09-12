import test from "node:test";
import assert from "node:assert/strict";
import {TabBridge, platformForUrl, serviceOrigin} from "./bridge.mjs";

function event() {
  const listeners = [];
  return {addListener: fn => listeners.push(fn), emit: (...args) => listeners.forEach(fn => fn(...args))};
}

function fixture(url = "https://item.jd.com/123.html") {
  const calls = [];
  const state = new Map();
  const tab = {id: 7, url};
  let nativeId = 0;
  const chrome = {
    storage: {session: {
      setAccessLevel: async () => {},
      set: async values => Object.entries(values).forEach(([key, value]) => state.set(key, value)),
      remove: async key => state.delete(key),
    }},
    tabs: {get: async id => { assert.equal(id, 7); return {...tab}; }, onUpdated: event(), onRemoved: event()},
    debugger: {
      onEvent: event(), onDetach: event(),
      attach: async (...args) => calls.push(["attach", ...args]),
      detach: async (...args) => calls.push(["detach", ...args]),
      sendCommand: async (target, method, params = {}) => {
        calls.push([method, target, params]);
        if (method === "Target.getTargetInfo") return {targetInfo: {targetId: "native-target", url: tab.url}};
        if (method === "Target.attachToTarget") return {sessionId: `native-${++nativeId}`};
        return {value: "ok"};
      },
    },
  };
  class Socket {
    static OPEN = 1;
    static CLOSING = 2;
    constructor(url) { this.url = url; this.readyState = 0; this.listeners = {}; this.sent = []; Socket.last = this; }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    emit(name, data) { (this.listeners[name] || []).forEach(fn => fn(data)); }
    send(data) { this.sent.push(JSON.parse(data)); }
    close() { this.readyState = 3; this.emit("close"); }
  }
  const bridge = new TabBridge(chrome, Socket, {
    setInterval() { assert.equal(this, globalThis); return 1; },
    clearInterval() { assert.equal(this, globalThis); },
  });
  const connect = async () => {
    await bridge.connect({service: "http://127.0.0.1:8766", token: "a".repeat(32), tabId: 7});
    Socket.last.readyState = 1;
    Socket.last.emit("open");
    await bridge.receive(JSON.stringify({type: "paired"}), bridge.generation);
    return Socket.last;
  };
  let next = 0;
  const command = (method, params = {}, sessionId) => bridge.receive(JSON.stringify({id: ++next, method, params, ...(sessionId ? {sessionId} : {})}), bridge.generation);
  return {bridge, calls, state, tab, chrome, Socket, connect, command};
}

test("platform and local-service allowlists reject foreign stores, Tmall, credentials and deceptive hosts", () => {
  for (const url of ["https://www.apple.com.cn/shop/buy-iphone/", "https://secure8.www.apple.com.cn/shop/checkout", "https://idmsa.apple.com/appleauth/auth", "https://www.apple.com/cn/shop/bag", "https://secure8.www.apple.com/shop/signIn/account"]) assert.equal(platformForUrl(url), "apple");
  assert.equal(platformForUrl("https://cart.taobao.com/cart.htm"), "taobao");
  assert.equal(platformForUrl("https://pc-settlement-lite-pro.pf.jd.com/"), "jd");
  for (const url of ["https://www.apple.com/shop/bag", "https://www.apple.com/uk/shop/bag", "https://detail.tmall.com/", "https://item.jd.com.evil.invalid/", "https://item.jd.com@evil.invalid/", "http://item.jd.com/", "https://item.jd.com:444/", "javascript:alert(1)", "file:///tmp/test"]) assert.equal(platformForUrl(url), null, url);
  assert.equal(serviceOrigin("http://localhost:8766"), "http://localhost:8766");
  for (const url of ["https://localhost:8766", "http://127.0.0.2", "http://localhost.evil.invalid", "http://user@localhost", "http://localhost/?token=x"]) assert.throws(() => serviceOrigin(url));
});

test("pair only the chosen tab; token is removed after auth; primary and guard CDP sessions route independently once", async () => {
  const f = fixture();
  const socket = await f.connect();
  assert.equal(f.bridge.status().connected, true);
  assert.equal(f.state.has("pairingToken"), false);
  assert.deepEqual(socket.sent[0], {token: "a".repeat(32)});
  assert.equal(socket.sent[1].platform, "jd");
  await f.command("Target.setAutoAttach", {autoAttach: true, flatten: true});
  assert.ok(socket.sent.find(message => message.method === "Target.attachedToTarget").params.targetInfo.browserContextId);
  const main = "session-jd-7";
  await f.command("Target.attachToBrowserTarget");
  const browserSession = socket.sent.at(-1).result.sessionId;
  await f.command("Target.attachToTarget", {targetId: f.bridge.binding.targetId, flatten: true}, browserSession);
  const secondary = socket.sent.at(-1).result.sessionId;
  await f.command("Input.dispatchMouseEvent", {type: "mousePressed", x: 12, y: 23, button: "left", clickCount: 1}, main);
  await f.command("Fetch.enable", {patterns: [{resourceType: "Document"}]}, secondary);
  assert.equal(f.calls.filter(call => call[0] === "Input.dispatchMouseEvent").length, 1);
  assert.deepEqual(f.calls.find(call => call[0] === "Fetch.enable")[1], {tabId: 7});
  await f.bridge.onEvent({tabId: 7}, "Page.loadEventFired", {timestamp: 1});
  assert.equal(socket.sent.at(-1).sessionId, main);
  await f.bridge.onEvent({tabId: 7}, "Fetch.requestPaused", {requestId: "r1", resourceType: "Document", request: {url: f.tab.url}});
  assert.equal(socket.sent.at(-1).sessionId, secondary);
  await f.command("Target.detachFromTarget", {sessionId: secondary}, browserSession);
  assert.equal(f.bridge.sessions.has(secondary), false);
  assert.equal(f.bridge.sessions.has(main), true);
});

test("duplicate action IDs disconnect without a second dispatch or auto reconnect", async () => {
  const f = fixture();
  const socket = await f.connect();
  await f.command("Target.setAutoAttach", {autoAttach: true});
  const message = JSON.stringify({id: 100, method: "Input.dispatchMouseEvent", params: {type: "mousePressed"}, sessionId: "session-jd-7"});
  const generation = f.bridge.generation;
  await f.bridge.receive(message, generation);
  await f.bridge.receive(message, generation);
  assert.equal(f.calls.filter(call => call[0] === "Input.dispatchMouseEvent").length, 1);
  assert.equal(f.bridge.status().connected, false);
  assert.equal(socket.readyState, 3);
  await f.bridge.receive(message, generation);
  assert.equal(f.calls.filter(call => call[0] === "Input.dispatchMouseEvent").length, 1);
  assert.equal(f.calls.filter(call => call[0] === "detach").length, 1);
});

test("cookies, HTTP replay, new tabs, close browser, external navigation and unsafe overrides are never forwarded", async () => {
  const f = fixture();
  const socket = await f.connect();
  await f.command("Target.setAutoAttach", {autoAttach: true});
  const count = f.calls.length;
  for (const method of ["Network.getAllCookies", "Network.getCookies", "Network.setCookies", "Storage.getCookies", "DOMStorage.getDOMStorageItems", "Network.replayXHR", "Network.getResponseBody", "Fetch.fulfillRequest", "Security.setIgnoreCertificateErrors", "Page.setBypassCSP", "DOM.setFileInputFiles"]) {
    await f.command(method, {}, "session-jd-7");
    assert.ok(socket.sent.at(-1).error, method);
  }
  for (const method of ["Target.createTarget", "Target.closeTarget", "Target.createBrowserContext", "Target.disposeBrowserContext", "Browser.close", "SystemInfo.getInfo"]) {
    await f.command(method);
    assert.ok(socket.sent.at(-1).error, method);
  }
  await f.command("Page.navigate", {url: "https://item.taobao.com/item.htm"}, "session-jd-7");
  assert.ok(socket.sent.at(-1).error);
  await f.command("Fetch.continueRequest", {requestId: "r1", postData: "order"}, "session-jd-7");
  assert.ok(socket.sent.at(-1).error);
  await f.command("Fetch.enable", {handleAuthRequests: true}, "session-jd-7");
  assert.ok(socket.sent.at(-1).result);
  assert.equal(f.calls.length, count + 1);
  assert.equal(f.calls.at(-1)[0], "Fetch.enable");
  assert.equal(f.calls.at(-1)[2].handleAuthRequests, undefined);
});

test("changed current/pending URL stops commands; lost relay leaves the shopping tab open and does not reconnect", async () => {
  const f = fixture();
  const socket = await f.connect();
  await f.command("Target.setAutoAttach", {autoAttach: true});
  f.tab.pendingUrl = "https://example.org/";
  await f.command("Input.insertText", {text: "must not type"}, "session-jd-7");
  assert.equal(f.calls.some(call => call[0] === "Input.insertText"), false);
  assert.equal(f.bridge.status().connected, false);
  assert.equal(socket.readyState, 3);
  assert.equal(f.calls.filter(call => call[0] === "detach").length, 1);
  assert.equal(f.calls.some(call => call[0] === "Target.closeTarget"), false);
});

test("network events omit session secrets and unrelated debugger targets", async () => {
  const f = fixture();
  const socket = await f.connect();
  await f.command("Target.setAutoAttach", {autoAttach: true});
  const start = socket.sent.length;
  await f.bridge.onEvent({tabId: 99}, "Runtime.consoleAPICalled", {});
  await f.bridge.onEvent({tabId: 7}, "Network.requestWillBeSentExtraInfo", {associatedCookies: [{value: "secret"}]});
  assert.equal(socket.sent.length, start);
  await f.bridge.onEvent({tabId: 7}, "Network.requestWillBeSent", {request: {url: f.tab.url, headers: {Cookie: "secret", Authorization: "secret", Accept: "text/html"}, postData: "sensitive", postDataEntries: ["secret"]}});
  const body = JSON.stringify(socket.sent.at(-1));
  assert.equal(body.includes("secret"), false);
  assert.equal(body.includes("sensitive"), false);
  assert.equal(socket.sent.at(-1).params.request.headers.Accept, "text/html");
  for (const key of ["redirectResponse", "response"]) {
    await f.bridge.onEvent({tabId: 7}, key === "response" ? "Network.responseReceived" : "Network.requestWillBeSent", {
      [key]: {headers: {"Set-Cookie": "secret", "Content-Type": "text/html"},
        headersText: "Set-Cookie: secret", requestHeaders: {Cookie: "secret"},
        requestHeadersText: "Cookie: secret"},
    });
    assert.equal(JSON.stringify(socket.sent.at(-1)).includes("secret"), false);
    assert.equal(socket.sent.at(-1).params[key].headers["Content-Type"], "text/html");
  }
});

test("one binding cannot be replaced while connected, and service worker recreation starts disconnected", async () => {
  const f = fixture();
  await f.connect();
  await assert.rejects(f.bridge.connect({service: "http://localhost:8766", token: "b".repeat(32), tabId: 7}));
  assert.equal(f.calls.filter(call => call[0] === "attach").length, 1);
  const restarted = new TabBridge(f.chrome, f.Socket, {setInterval: () => 1, clearInterval: () => {}});
  assert.equal(restarted.status().connected, false);
  assert.equal(restarted.binding, null);
});

test("concurrent connect and cancellation during native attach cannot leave an attached debugger", async () => {
  const f = fixture();
  let finishAttach;
  f.chrome.debugger.attach = () => new Promise(resolve => { finishAttach = resolve; });
  const args = {service: "http://localhost:8766", token: "b".repeat(32), tabId: 7};
  const connecting = f.bridge.connect(args);
  await assert.rejects(f.bridge.connect(args), /先断开/);
  while (!finishAttach) await new Promise(resolve => setImmediate(resolve));
  let closed = false;
  const closing = f.bridge.disconnect().then(() => { closed = true; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(closed, false);
  await assert.rejects(f.bridge.connect(args), /清理完成/);
  finishAttach();
  await closing;
  await assert.rejects(connecting);
  assert.equal(f.bridge.binding, null);
  assert.equal(f.bridge.socket, null);
  assert.equal(f.calls.filter(call => call[0] === "detach").length, 1);
});

test("disconnect reentry shares cleanup and rejects reconnect until delayed storage and detach finish", async () => {
  const f = fixture();
  await f.connect();
  const originalRemove = f.chrome.storage.session.remove;
  const originalDetach = f.chrome.debugger.detach;
  let releaseStorage;
  let releaseDetach;
  f.chrome.storage.session.remove = async key => {
    await new Promise(resolve => { releaseStorage = resolve; });
    return originalRemove(key);
  };
  f.chrome.debugger.detach = async (...args) => {
    await new Promise(resolve => { releaseDetach = resolve; });
    return originalDetach(...args);
  };
  const closing = f.bridge.disconnect();
  assert.equal(f.bridge.disconnect(), closing);
  while (!releaseStorage) await new Promise(resolve => setImmediate(resolve));
  const args = {service: "http://localhost:8766", token: "b".repeat(32), tabId: 7};
  await assert.rejects(f.bridge.connect(args), /清理完成/);
  assert.equal(f.calls.filter(call => call[0] === "attach").length, 1);
  releaseStorage();
  while (!releaseDetach) await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.bridge.disconnect(), closing);
  await assert.rejects(f.bridge.connect(args), /清理完成/);
  releaseDetach();
  await closing;
  assert.equal(f.bridge.closing, null);
  assert.equal(f.calls.filter(call => call[0] === "detach").length, 1);
  f.chrome.storage.session.remove = originalRemove;
  f.chrome.debugger.detach = originalDetach;
  await f.connect();
  assert.equal(f.bridge.status().connected, true);
  assert.equal(f.calls.filter(call => call[0] === "attach").length, 2);
  assert.equal(f.calls.filter(call => call[0] === "detach").length, 1);
});

test("stale tab reads, socket errors and debugger callbacks cannot disconnect a replacement binding", async () => {
  for (const source of ["current", "socket", "debugger"]) {
    const f = fixture();
    if (source === "socket") {
      await f.bridge.connect({service: "http://localhost:8766", token: "a".repeat(32), tabId: 7});
      f.Socket.last.readyState = 1;
      f.Socket.last.emit("open");
    } else await f.connect();
    const originalGet = f.chrome.tabs.get;
    const originalEvent = f.bridge.onEvent;
    let release;
    let rejected;
    if (source === "debugger") {
      f.bridge.onEvent = () => new Promise((resolve, reject) => { release = () => reject(new Error("old event failed")); });
      f.chrome.debugger.onEvent.emit({tabId: 7}, "Page.loadEventFired", {});
    } else {
      f.chrome.tabs.get = () => new Promise(resolve => { release = () => resolve({id: 7, url: "https://example.org/old-page"}); });
      if (source === "current") rejected = assert.rejects(f.bridge.current(), /detached/);
      else f.Socket.last.emit("message", {data: JSON.stringify({type: "paired"})});
    }
    await f.bridge.disconnect();
    f.chrome.tabs.get = originalGet;
    f.bridge.onEvent = originalEvent;
    await f.connect();
    const replacement = f.bridge.binding;
    release();
    if (rejected) await rejected;
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.bridge.binding, replacement, source);
    assert.equal(f.bridge.status().connected, true, source);
    assert.equal(f.calls.filter(call => call[0] === "detach").length, 1, source);
  }
});

test("shared-tab Fetch waits for every subscriber, denial wins, and lost subscribers fail pending requests", async () => {
  for (const outcome of ["allow", "deny-first", "deny-last", "detach", "disable-approved", "disconnect"]) {
    const f = fixture();
    const socket = await f.connect();
    await f.command("Target.setAutoAttach", {autoAttach: true});
    const main = f.bridge.binding.mainSession;
    await f.command("Target.attachToTarget", {targetId: f.bridge.binding.targetId, flatten: true});
    const guard = socket.sent.at(-1).result.sessionId;
    await f.command("Fetch.enable", {patterns: [{urlPattern: "*"}]}, main);
    await f.command("Fetch.enable", {patterns: [{resourceType: "Document", requestStage: "Request"}]}, guard);
    await f.bridge.onEvent({tabId: 7}, "Fetch.requestPaused", {requestId: "r1", resourceType: "Document", request: {url: f.tab.url}});
    assert.equal(socket.sent.filter(message => message.method === "Fetch.requestPaused").length, 2);
    assert.equal(f.calls.filter(call => call[0] === "Fetch.continueRequest").length, 0);
    if (outcome === "deny-first") {
      await f.command("Fetch.failRequest", {requestId: "r1", errorReason: "BlockedByClient"}, guard);
      await f.command("Fetch.continueRequest", {requestId: "r1"}, main);
    } else {
      await f.command("Fetch.continueRequest", {requestId: "r1"}, main);
      assert.equal(f.calls.filter(call => call[0] === "Fetch.continueRequest").length, 0);
      if (outcome === "allow") {
        await f.command("Fetch.continueRequest", {requestId: "r1"}, guard);
        await f.command("Fetch.continueRequest", {requestId: "r1"}, guard);
      } else if (outcome === "deny-last") {
        await f.command("Fetch.failRequest", {requestId: "r1", errorReason: "BlockedByClient"}, guard);
      } else if (outcome === "detach") {
        await f.command("Target.detachFromTarget", {sessionId: guard});
      } else if (outcome === "disable-approved") {
        await f.command("Fetch.disable", {}, main);
      } else await f.bridge.disconnect();
    }
    assert.equal(f.calls.filter(call => call[0] === "Fetch.continueRequest").length, outcome === "allow" ? 1 : 0, outcome);
    assert.equal(f.calls.filter(call => call[0] === "Fetch.failRequest").length, outcome === "allow" ? 0 : 1, outcome);
    assert.equal(f.calls.filter(call => call[0] === "Target.attachToTarget").length, 0);
  }
});
