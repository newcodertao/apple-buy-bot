const $ = id => document.getElementById(id);
let busy = false;

function show(result) {
  $("status").textContent = result.message || "等待连接";
  const bound = result.connected || result.connecting;
  $("connect").disabled = busy || bound;
  $("disconnect").disabled = busy || !bound;
  $("service").disabled = bound;
  $("token").disabled = bound;
  if (bound && result.service) $("service").value = result.service;
  if (result.connected) $("token").value = "";
}

async function send(type, extra = {}) {
  const result = await chrome.runtime.sendMessage({type, ...extra});
  if (!result?.ok) throw new Error(result?.message || "扩展服务未响应，请重新打开扩展");
  return result;
}

async function act(type) {
  busy = true;
  $("connect").disabled = true;
  $("disconnect").disabled = true;
  try {
    const result = await send(type, {service: $("service").value.trim(), token: $("token").value.trim()});
    busy = false;
    show(result);
  } catch (error) {
    busy = false;
    show({message: error.message});
  }
}

$("pair-form").addEventListener("submit", event => { event.preventDefault(); void act("connect"); });
$("disconnect").addEventListener("click", () => { void act("disconnect"); });
$("console").addEventListener("click", () => { void act("console"); });
await send("status").then(show).catch(error => show({message: error.message}));
setInterval(() => { if (!busy) void send("status").then(show).catch(() => {}); }, 1000);
