import {TabBridge, serviceOrigin} from "./bridge.mjs";

// Worker restart intentionally forgets the connection. Nothing resumes automatically.
// Chrome extension service workers reject top-level await. Register listeners
// synchronously, then wait for this initialization within the message handler.
const initialized = (async () => {
  await chrome.storage.session.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"});
  await chrome.storage.session.remove("pairingToken");
})();
const bridge = new TabBridge(chrome, WebSocket);

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL("popup.html")) return false;
  (async () => {
    await initialized;
    if (message.type === "status") return bridge.status();
    if (message.type === "disconnect") return bridge.disconnect();
    if (message.type === "connect") {
      const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
      if (!tab?.id) throw new Error("请先选择一个购物标签页");
      return bridge.connect({...message, tabId: tab.id});
    }
    if (message.type === "console") {
      await chrome.tabs.create({url: serviceOrigin(message.service)});
      return bridge.status();
    }
    throw new Error("未知操作");
  })().then(result => respond({ok: true, ...result}), error => respond({ok: false, message: error.message}));
  return true;
});
