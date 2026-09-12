const LABELS = {apple: "Apple 中国大陆", jd: "京东", taobao: "淘宝"};

export function platformForUrl(value) {
  let url;
  try { url = new URL(value); } catch { return null; }
  if (url.protocol !== "https:" || url.port || url.username || url.password) return null;
  const host = url.hostname;
  if (host === "jd.com" || host.endsWith(".jd.com")) return "jd";
  if (host === "taobao.com" || host.endsWith(".taobao.com")) return "taobao";
  if (["apple.com.cn", "www.apple.com.cn"].includes(host) || /^secure\d*\.www\.apple\.com\.cn$/.test(host)) return "apple";
  if (["account.apple.com", "appleid.apple.com", "idmsa.apple.com"].includes(host)) return "apple";
  if (["apple.com", "www.apple.com"].includes(host) && /^\/cn(?:\/|$)/.test(url.pathname)) return "apple";
  if (/^secure\d*\.www\.apple\.com$/.test(host) && /^\/shop\/signIn(?:\/|$)/i.test(url.pathname)) return "apple";
  return null;
}

export function serviceOrigin(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error("请输入本机控制台地址"); }
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname) || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new Error("控制台仅允许 http://127.0.0.1:端口 或 http://localhost:端口");
  }
  return url.origin;
}

// Only commands used for the existing visible-page adapter are forwarded.
// Cookie / storage export, network replay and browser-wide control are absent.
const FORWARDED = new Set([
  "Accessibility.getFullAXTree", "Accessibility.getPartialAXTree", "Accessibility.enable", "Accessibility.disable",
  "DOM.describeNode", "DOM.resolveNode", "DOM.getDocument", "DOM.getContentQuads", "DOM.getBoxModel", "DOM.getOuterHTML",
  "DOM.scrollIntoViewIfNeeded", "DOM.focus", "DOM.enable", "DOM.disable", "DOM.getFrameOwner", "DOM.querySelector", "DOM.querySelectorAll",
  "Input.dispatchKeyEvent", "Input.dispatchMouseEvent", "Input.dispatchTouchEvent", "Input.insertText", "Input.setIgnoreInputEvents",
  "Page.enable", "Page.disable", "Page.getFrameTree", "Page.getLayoutMetrics", "Page.setLifecycleEventsEnabled",
  "Page.createIsolatedWorld", "Page.addScriptToEvaluateOnNewDocument", "Page.removeScriptToEvaluateOnNewDocument",
  "Page.navigate", "Page.reload", "Page.stopLoading", "Page.bringToFront", "Page.handleJavaScriptDialog",
  "Page.captureScreenshot", "Page.setInterceptFileChooserDialog", "Page.setFontFamilies",
  "Runtime.enable", "Runtime.disable", "Runtime.evaluate", "Runtime.callFunctionOn", "Runtime.getProperties",
  "Runtime.releaseObject", "Runtime.releaseObjectGroup", "Runtime.addBinding", "Runtime.removeBinding", "Runtime.runIfWaitingForDebugger",
  "Network.enable", "Network.disable", "Network.setCacheDisabled", "Network.setBypassServiceWorker",
  "Log.enable", "Log.disable",
  "Emulation.setFocusEmulationEnabled", "Emulation.setEmulatedMedia", "Emulation.setDeviceMetricsOverride", "Emulation.clearDeviceMetricsOverride",
  "Fetch.enable", "Fetch.disable", "Fetch.continueRequest", "Fetch.failRequest",
  "Target.setAutoAttach",
]);

const omitHeaders = (headers = {}) => Object.fromEntries(Object.entries(headers).filter(([key]) => !/cookie|authorization|token/i.test(key)));

function safeEvent(method, params) {
  // ExtraInfo includes associated Cookie values even when Cookie methods are blocked.
  if (method.endsWith("ExtraInfo")) return null;
  if (!method.startsWith("Network.") && method !== "Fetch.requestPaused") return params;
  const result = structuredClone(params);
  for (const key of ["request", "response", "redirectResponse"]) {
    if (!result[key]) continue;
    result[key].headers = omitHeaders(result[key].headers);
    delete result[key].headersText;
    delete result[key].requestHeaders;
    delete result[key].requestHeadersText;
    delete result[key].postData;
    delete result[key].postDataEntries;
  }
  delete result.responseHeaders;
  return result;
}

function matchesRequest(pattern, event) {
  if (pattern.resourceType && pattern.resourceType !== event.resourceType) return false;
  const glob = pattern.urlPattern || "*";
  const expression = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".");
  return new RegExp(`^${expression}$`).test(event.request.url);
}

export class TabBridge {
  constructor(chrome, WebSocketImpl, {setInterval: interval = globalThis.setInterval, clearInterval: clear = globalThis.clearInterval} = {}) {
    this.chrome = chrome;
    this.WebSocket = WebSocketImpl;
    this.interval = interval.bind(globalThis);
    this.clear = clear.bind(globalThis);
    this.binding = null;
    this.socket = null;
    this.message = "未连接；先在购物标签页完成登录";
    this.sessions = new Map();
    this.fetchPatterns = new Map();
    this.fetchRequests = new Map();
    this.fetchFinished = new Set();
    this.fetchUpdate = Promise.resolve();
    this.seen = new Set();
    this.serial = 0;
    this.generation = 0;
    this.ready = false;
    this.connecting = false;
    this.closing = null;
    chrome.debugger.onEvent.addListener((source, method, params) => {
      const binding = this.binding;
      void this.onEvent(source, method, params).catch(() => {
        if (binding && this.binding === binding) return this.disconnect("页面连接中断，请重新连接");
      });
    });
    chrome.debugger.onDetach.addListener(source => {
      if (source.tabId === this.binding?.tabId) void this.disconnect("调试连接已断开；页面保留，任务须在控制台核对");
    });
    chrome.tabs.onRemoved.addListener(tabId => {
      if (tabId === this.binding?.tabId) void this.disconnect("所选标签页已关闭");
    });
    chrome.tabs.onUpdated.addListener((tabId, change) => {
      if (tabId !== this.binding?.tabId || !change.url) return;
      if (platformForUrl(change.url) !== this.binding.platform) void this.disconnect("页面离开已选平台，已停止连接；请人工核对");
    });
  }

  status() {
    return {connected: this.ready, connecting: this.connecting || Boolean(this.closing), platform: this.binding?.platform || null, label: LABELS[this.binding?.platform] || "", service: this.binding?.origin || null, message: this.message};
  }

  async current() {
    const binding = this.binding;
    if (!binding) throw new Error("没有已批准的标签页");
    const tab = await this.chrome.tabs.get(binding.tabId);
    if (this.binding !== binding) throw new Error("Page session is detached");
    // activeTab grants the initial origin. After a normal same-platform host
    // change, debugger still owns ONLY this tab and supplies its current URL.
    if (!tab.url && binding.attached) {
      const result = await this.chrome.debugger.sendCommand({tabId: binding.tabId}, "Target.getTargetInfo");
      if (this.binding !== binding) throw new Error("Page session is detached");
      tab.url = result.targetInfo?.url;
    }
    if (platformForUrl(tab.url) !== binding.platform || (tab.pendingUrl && platformForUrl(tab.pendingUrl) !== binding.platform)) {
      await this.disconnect("页面离开已选平台，已停止连接；请人工核对");
      throw new Error("Page left the approved platform");
    }
    return tab;
  }

  async connect({service, token, tabId}) {
    if (this.binding || this.connecting || this.closing) throw new Error("请先断开当前标签页，并等待清理完成后再连接");
    const origin = serviceOrigin(service);
    if (typeof token !== "string" || !/^[A-Za-z0-9_-]{24,256}$/.test(token)) throw new Error("请粘贴本机控制台生成的配对码");
    // Reserve synchronously: two clicks must not attach two debuggers while
    // tabs.get is pending, and disconnect must cancel a pending connection.
    this.connecting = true;
    const generation = ++this.generation;
    let tab;
    try {
      tab = await this.chrome.tabs.get(tabId);
      if (generation !== this.generation) throw new Error("连接已取消");
    } catch (error) {
      if (generation === this.generation) this.connecting = false;
      throw error;
    }
    const platform = platformForUrl(tab.url);
    if (!platform || (tab.pendingUrl && platformForUrl(tab.pendingUrl) !== platform)) {
      this.connecting = false;
      throw new Error("请切换到淘宝、京东或 Apple 中国大陆网页，再打开扩展");
    }
    this.message = "正在连接所选标签页…";
    const binding = this.binding = {tabId, platform, origin, targetId: `${platform}-${tabId}`, mainSession: `session-${platform}-${tabId}`};
    try {
      binding.pending = this.chrome.storage.session.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"});
      await binding.pending;
      if (generation !== this.generation) throw new Error("连接已取消");
      binding.pending = this.chrome.storage.session.set({pairingToken: token});
      await binding.pending;
      if (generation !== this.generation) throw new Error("连接已取消");
      binding.pending = this.chrome.debugger.attach({tabId}, "1.3").then(() => { binding.attached = true; });
      await binding.pending;
      if (generation !== this.generation) throw new Error("连接已取消");
      const {targetInfo} = await this.chrome.debugger.sendCommand({tabId}, "Target.getTargetInfo");
      binding.nativeTarget = targetInfo.targetId;
      // Playwright identifies the main frame by its real target ID. Inventing
      // a different ID makes a live frame appear detached during initialization.
      binding.targetId = targetInfo.targetId;
      binding.contextId = targetInfo.browserContextId || "approved-default-context";
      await this.current();
      if (generation !== this.generation) throw new Error("连接已取消");
      const socket = this.socket = new this.WebSocket(`${origin.replace(/^http:/, "ws:")}/extension/relay`);
      socket.addEventListener("open", () => {
        if (this.socket === socket) socket.send(JSON.stringify({token}));
      });
      socket.addEventListener("message", event => {
        if (this.socket === socket) void this.receive(event.data, generation).catch(() => {
          if (this.socket === socket && this.generation === generation) return this.disconnect("连接协议异常，已停止；请重新配对");
        });
      });
      socket.addEventListener("close", () => {
        if (this.socket === socket) void this.disconnect("本机控制台连接已断开；不会自动重试动作");
      });
      socket.addEventListener("error", () => {
        if (this.socket === socket) void this.disconnect("无法连接本机控制台，请检查地址和配对码");
      });
      this.heartbeat = this.interval(() => {
        if (this.ready) this.send({type: "ping"});
      }, 20000);
      return this.status();
    } catch (error) {
      if (generation === this.generation) await this.disconnect("无法连接此标签页。请关闭该页的开发者工具，检查控制台与配对码后重试");
      else if (this.closing) await this.closing;
      // A detach request can arrive while native attach is still resolving.
      if (binding.attached) await this.chrome.debugger.detach({tabId}).catch(() => {});
      if (/Cannot access|not allowed|permission/i.test(error.message)) this.message = "浏览器未授予当前标签页访问权。请返回购物页，点击工具栏里的扩展图标重新连接";
      else if (/Another debugger|already attached/i.test(error.message)) this.message = "此标签页已被开发者工具或其他调试程序连接，请先断开它们";
      throw new Error(this.message);
    }
  }

  send(message) {
    if (this.socket?.readyState === this.WebSocket.OPEN) this.socket.send(JSON.stringify(message));
  }

  async receive(raw, generation) {
    if (generation !== this.generation) return;
    const message = JSON.parse(raw);
    if (message.type === "paired" && !this.ready) {
      await this.current();
      if (generation !== this.generation) return;
      this.ready = true;
      this.connecting = false;
      this.message = `${LABELS[this.binding.platform]}已连接；请在本机控制台继续`;
      const binding = this.binding;
      binding.pending = this.chrome.storage.session.remove("pairingToken");
      await binding.pending;
      if (generation !== this.generation) return;
      this.send({type: "bound", platform: this.binding.platform, targetId: this.binding.targetId, tabId: this.binding.tabId});
      return;
    }
    if (message.type === "pong") return;
    if (!this.ready || !Number.isSafeInteger(message.id) || message.id < 0 || typeof message.method !== "string") throw new Error("Invalid relay message");
    // A duplicate request is a protocol loss, never permission to repeat a click.
    if (this.seen.has(message.id)) return this.disconnect("收到重复动作编号，已断开；请在控制台核对任务");
    this.seen.add(message.id);
    try {
      const result = await this.command(message);
      if (generation === this.generation) this.send({id: message.id, ...(message.sessionId ? {sessionId: message.sessionId} : {}), result: result || {}});
    } catch (error) {
      if (generation === this.generation) this.send({id: message.id, ...(message.sessionId ? {sessionId: message.sessionId} : {}), error: {code: -32000, message: error.message}});
    }
  }

  targetInfo(tab) {
    return {targetId: this.binding.targetId, browserContextId: this.binding.contextId, type: "page", title: "已批准的购物标签页", url: tab.url, attached: this.sessions.has(this.binding.mainSession), canAccessOpener: false};
  }

  async command({method, params = {}, sessionId}) {
    const tab = await this.current();
    const binding = this.binding;
    if (sessionId && !this.sessions.has(sessionId)) throw new Error("Unknown or detached session");
    if (!sessionId || this.sessions.get(sessionId)?.browser) {
      switch (method) {
        case "Browser.getVersion": {
          const userAgent = globalThis.navigator?.userAgent || "Chrome/125.0.0.0";
          return {protocolVersion: "1.3", product: `Chrome/${/Chrome\/([\d.]+)/.exec(userAgent)?.[1] || "125.0.0.0"}`, revision: "", userAgent, jsVersion: ""};
        }
        case "Browser.setDownloadBehavior":
          if (!["deny", "default"].includes(params.behavior)) throw new Error("Extension does not enable downloads");
          return {};
        case "Target.getBrowserContexts": return {browserContextIds: []};
        case "Target.attachToBrowserTarget": {
          const logical = `${binding.mainSession}-browser`;
          this.sessions.set(logical, {browser: true});
          return {sessionId: logical};
        }
        case "Target.getTargets": return {targetInfos: [this.targetInfo(tab)]};
        case "Target.getTargetInfo":
          if (params.targetId && params.targetId !== binding.targetId) throw new Error("Target is not approved");
          return {targetInfo: this.targetInfo(tab)};
        case "Target.setDiscoverTargets":
          if (params.discover) this.send({method: "Target.targetCreated", params: {targetInfo: this.targetInfo(tab)}});
          return {};
        case "Target.setAutoAttach":
          if (params.autoAttach && !this.sessions.has(binding.mainSession)) {
            this.sessions.set(binding.mainSession, {native: undefined});
            this.send({method: "Target.attachedToTarget", params: {sessionId: binding.mainSession, targetInfo: this.targetInfo(tab), waitingForDebugger: false}});
          }
          return {};
        case "Target.attachToTarget": {
          if (params.targetId !== binding.targetId || params.flatten !== true) throw new Error("Target is not approved or flatten is required");
          const logical = `${binding.mainSession}-${++this.serial}`;
          // Chrome refuses a second native attachment to this same target.
          // Logical sessions share only the chosen tab; Fetch approvals below
          // must all complete before its one native request can continue.
          this.sessions.set(logical, {native: undefined, parent: sessionId, alias: true});
          return {sessionId: logical};
        }
        case "Target.detachFromTarget": {
          const session = this.sessions.get(params.sessionId);
          if (!session || params.sessionId === binding.mainSession) throw new Error("Cannot detach the approved page session");
          await this.setFetchPatterns(params.sessionId, null);
          this.sessions.delete(params.sessionId);
          if (session.native) await this.chrome.debugger.sendCommand({tabId: binding.tabId}, "Target.detachFromTarget", {sessionId: session.native});
          this.send({...(sessionId ? {sessionId} : {}), method: "Target.detachedFromTarget", params: {sessionId: params.sessionId, targetId: binding.targetId}});
          return {};
        }
        default: throw new Error("Browser command is not permitted");
      }
    }
    const session = this.sessions.get(sessionId);
    if (session.url && platformForUrl(session.url) !== binding.platform) throw new Error("Frame is not approved");
    if (method === "Target.getTargetInfo") return {targetInfo: this.targetInfo(tab)};
    if (!FORWARDED.has(method)) throw new Error("Command is outside visible-page control");
    if (method === "Page.navigate" && platformForUrl(params.url) !== binding.platform) throw new Error("Navigation is outside the approved platform");
    if (method === "Fetch.continueRequest" && Object.keys(params).some(key => key !== "requestId" && key !== "interceptResponse")) throw new Error("Changing or replaying an HTTP request is not permitted");
    // Playwright always requests auth events alongside route interception.
    // Only patterns are forwarded; HTTP authentication stays with the browser.
    if (method === "Fetch.enable") return this.setFetchPatterns(sessionId, params.patterns || [{}]);
    if (method === "Fetch.disable") return this.setFetchPatterns(sessionId, null);
    if (method === "Fetch.continueRequest" || method === "Fetch.failRequest") return this.decideFetch(sessionId, method, params);
    return this.chrome.debugger.sendCommand({tabId: binding.tabId, ...(session.native ? {sessionId: session.native} : {})}, method, params);
  }

  async setFetchPatterns(sessionId, patterns) {
    const binding = this.binding;
    const session = this.sessions.get(sessionId);
    if (!binding || !session) throw new Error("Fetch session is detached");
    if (patterns && (!Array.isArray(patterns) || patterns.some(pattern =>
      (pattern.requestStage && pattern.requestStage !== "Request") ||
      (pattern.urlPattern && typeof pattern.urlPattern !== "string")))) throw new Error("Only existing browser requests can be checked");
    const update = async () => {
      if (this.binding !== binding) throw new Error("Fetch session is detached");
      if (patterns) this.fetchPatterns.set(sessionId, patterns);
      else {
        this.fetchPatterns.delete(sessionId);
        for (const [key, request] of this.fetchRequests) {
          if (request.participants.has(sessionId)) await this.finishFetch(key, request, "Fetch.failRequest", {requestId: request.id, errorReason: "BlockedByClient"}, binding);
        }
      }
      const union = [...this.fetchPatterns].filter(([logical]) => this.sessions.get(logical)?.native === session.native).flatMap(([, values]) => values);
      const target = {tabId: binding.tabId, ...(session.native ? {sessionId: session.native} : {})};
      await this.chrome.debugger.sendCommand(target, union.length ? "Fetch.enable" : "Fetch.disable", union.length ? {patterns: union} : {});
      return {};
    };
    this.fetchUpdate = this.fetchUpdate.catch(() => {}).then(update);
    return this.fetchUpdate;
  }

  async finishFetch(key, request, method, params, binding = this.binding) {
    if (this.fetchFinished.has(key)) return {};
    this.fetchRequests.delete(key);
    this.fetchFinished.add(key);
    if (!binding) throw new Error("Fetch session is detached");
    return this.chrome.debugger.sendCommand({tabId: binding.tabId, ...(request.native ? {sessionId: request.native} : {})}, method, params);
  }

  async decideFetch(sessionId, method, params) {
    if (params.interceptResponse) throw new Error("Response interception is not permitted");
    const native = this.sessions.get(sessionId)?.native;
    const key = `${native || "root"}:${params.requestId}`;
    if (this.fetchFinished.has(key)) return {};
    const request = this.fetchRequests.get(key);
    if (!request || !request.waiting.has(sessionId)) throw new Error("Unknown or already decided request");
    request.waiting.delete(sessionId);
    if (method === "Fetch.failRequest") return this.finishFetch(key, request, method, {requestId: request.id, errorReason: params.errorReason || "BlockedByClient"});
    if (request.waiting.size) return {};
    return this.finishFetch(key, request, "Fetch.continueRequest", {requestId: request.id});
  }

  async pausedFetch(source, params) {
    const binding = this.binding;
    const native = source.sessionId;
    const key = `${native || "root"}:${params.requestId}`;
    if (this.fetchRequests.has(key) || this.fetchFinished.has(key)) return;
    const waiting = new Set([...this.fetchPatterns].filter(([logical, patterns]) =>
      this.sessions.get(logical)?.native === native && patterns.some(pattern => matchesRequest(pattern, params))
    ).map(([logical]) => logical));
    const request = {id: params.requestId, native, waiting, participants: new Set(waiting)};
    this.fetchRequests.set(key, request);
    if (params.resourceType === "Document" && platformForUrl(params.request.url) !== binding.platform) {
      await this.finishFetch(key, request, "Fetch.failRequest", {requestId: request.id, errorReason: "BlockedByClient"}, binding);
      return this.disconnect("页面请求离开已选平台，已停止连接；请人工核对");
    }
    if (!waiting.size) return this.finishFetch(key, request, "Fetch.continueRequest", {requestId: request.id}, binding);
    const cleaned = safeEvent("Fetch.requestPaused", params);
    for (const logical of waiting) this.send({sessionId: logical, method: "Fetch.requestPaused", params: cleaned});
  }

  async onEvent(source, method, params = {}) {
    if (!this.ready || source.tabId !== this.binding?.tabId) return;
    const binding = this.binding;
    if (method === "Fetch.requestPaused") return this.pausedFetch(source, params);
    if (method === "Target.attachedToTarget") {
      // Manual second attachment is returned to its CDP caller, not a second page.
      if (params.targetInfo?.targetId === binding.nativeTarget) return;
      if (params.targetInfo?.type !== "iframe" || platformForUrl(params.targetInfo.url) !== binding.platform) {
        const child = {tabId: binding.tabId, sessionId: params.sessionId};
        await this.chrome.debugger.sendCommand(child, "Runtime.runIfWaitingForDebugger").catch(() => {});
        await this.chrome.debugger.sendCommand({tabId: binding.tabId}, "Target.detachFromTarget", {sessionId: params.sessionId}).catch(() => {});
        return;
      }
      const logical = `${binding.mainSession}-frame-${params.sessionId}`;
      this.sessions.set(logical, {native: params.sessionId, url: params.targetInfo.url});
      params = {...params, sessionId: logical};
    }
    let logical = binding.mainSession;
    if (source.sessionId) {
      logical = [...this.sessions].find(([, session]) => session.native === source.sessionId)?.[0];
      if (!logical) return;
    }
    if (method === "Target.detachedFromTarget") {
      const entry = [...this.sessions].find(([, session]) => session.native === params.sessionId);
      if (!entry) return;
      this.sessions.delete(entry[0]);
      params = {...params, sessionId: entry[0]};
      logical = entry[1].parent || logical;
    }
    if (method === "Page.frameNavigated") {
      const frameUrl = params.frame?.url;
      if (!params.frame?.parentId && platformForUrl(frameUrl) !== binding.platform) {
        if (source.sessionId) {
          const session = this.sessions.get(logical);
          if (session) session.url = frameUrl;
          return;
        }
        return this.disconnect("页面离开已选平台，已停止连接；请人工核对");
      }
    }
    const cleaned = safeEvent(method, params);
    if (cleaned !== null) this.send({sessionId: logical, method, params: cleaned});
  }

  disconnect(message = "已断开；购物标签页保留，任务和订单记录由本机控制台保留") {
    if (this.closing) return this.closing;
    ++this.generation;
    const binding = this.binding;
    const socket = this.socket;
    this.binding = null;
    this.socket = null;
    this.ready = false;
    this.connecting = false;
    this.sessions.clear();
    this.fetchPatterns.clear();
    this.seen.clear();
    this.message = "正在断开连接，请等待清理完成";
    this.clear(this.heartbeat);
    this.heartbeat = null;
    // Publish the shared closing promise before any asynchronous cleanup. A
    // second disconnect waits for it; no new binding can race old cleanup.
    this.closing = Promise.resolve().then(async () => {
      if (socket && socket.readyState < this.WebSocket.CLOSING) socket.close();
      await binding?.pending?.catch(() => {});
      await this.fetchUpdate.catch(() => {});
      for (const [key, request] of this.fetchRequests) {
        await this.finishFetch(key, request, "Fetch.failRequest", {requestId: request.id, errorReason: "BlockedByClient"}, binding).catch(() => {});
      }
      this.fetchRequests.clear();
      this.fetchFinished.clear();
      await this.chrome.storage.session.remove("pairingToken").catch(() => {});
      if (binding?.attached) {
        await this.chrome.debugger.detach({tabId: binding.tabId}).catch(() => {});
        binding.attached = false;
      }
    }).then(() => {
      this.closing = null;
      this.message = message;
      return this.status();
    });
    return this.closing;
  }
}
