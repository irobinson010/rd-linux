"use strict";

console.log("rdclient build 22 loaded");

const params = new URLSearchParams(location.search);
const TOKEN = params.get("token") || "";

const statusEl = document.getElementById("status");
const controlBtn = document.getElementById("control");
const fullscreenBtn = document.getElementById("fullscreen");
const video = document.getElementById("screen");
const stage = document.getElementById("stage");
const overlay = document.getElementById("overlay");
const overlayText = document.getElementById("overlay-text");
const monitorsEl = document.getElementById("monitors");
const fillBtn = document.getElementById("fill");
const soundBtn = document.getElementById("sound");
const resSel = document.getElementById("res");
const bitrateSel = document.getElementById("bitrate");
const zoomInBtn = document.getElementById("zoomin");
const zoomOutBtn = document.getElementById("zoomout");
const zoomResetBtn = document.getElementById("zoomreset");
const keyboardBtn = document.getElementById("keyboard");
const kbdInput = document.getElementById("kbdinput");
const menuBtn = document.getElementById("menubtn");
const controls = document.getElementById("controls");
let fillMode = false;
let zoom = 1, panX = 0, panY = 0;
// Video mode the browser requests. Firefox and Chromium-on-Linux/NVIDIA often
// can't decode H.264 in WebRTC at all, so we walk this chain until frames
// actually decode, and remember the winner per device:
//   high = H.264 High (best, needs HW decode) · baseline = H.264 baseline
//   vp8  = VP8 (software, decodes in EVERY browser -- the universal fallback)
const VMODES = ["high", "baseline", "vp8"];
let vmode = "high";
try {
  const saved = localStorage.getItem("rdvmode");
  if (VMODES.includes(saved)) vmode = saved;
} catch (e) { /* */ }
let decodedOk = false;       // any frame decoded this connection?
let fallbackTimer = null;    // fires if nothing decodes -> try the next mode

let pc = null;
let ws = null;
let inputChannel = null;     // datachannel created by the server ("input")
let controlling = false;
let statsTimer = null;
let remoteStream = null;     // one stream holding the video + audio tracks
let monitorList = [];        // [{index,width,height}, ...] from the server
let activeMonitor = 0;
const pressedKeys = new Set();     // codes sent down but not yet up
const pressedButtons = new Set();

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = cls || "";
}

function showOverlay(text) {
  overlayText.textContent = text;
  overlay.classList.remove("hidden");
}
function hideOverlay() { overlay.classList.add("hidden"); }

// ---- signaling -----------------------------------------------------------

let reconnecting = false;

// Monitor switches and resolution changes both reconnect with the new settings
// -- a clean fresh stream, far more reliable than reconfiguring it live.
function reconnect(statusMsg) {
  reconnecting = true;
  setStatus(statusMsg || "reconnecting…");
  teardown();
  if (ws) { try { ws.close(); } catch (e) { /* ignore */ } ws = null; }
  setTimeout(() => { reconnecting = false; connect(); }, 400);
}

// If nothing decodes within the window, the browser can't play this codec --
// advance to the next mode (high -> baseline -> vp8), remember it, reconnect.
function maybeFallback() {
  fallbackTimer = null;
  if (decodedOk) return;
  const i = VMODES.indexOf(vmode);
  if (i < VMODES.length - 1) {
    vmode = VMODES[i + 1];
    try { localStorage.setItem("rdvmode", vmode); } catch (e) { /* */ }
    console.log("no frames decoded -> trying video mode:", vmode);
    reconnect("trying compatible video (" + vmode + ")…");
  } else {
    setStatus("this browser can't decode the video", "err");
    showOverlay("This browser couldn't decode any offered video codec. Try " +
      "Chrome or Edge, or connect from another device.");
  }
}

function connect() {
  decodedOk = false;
  if (fallbackTimer) clearTimeout(fallbackTimer);
  fallbackTimer = setTimeout(maybeFallback, 9000);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const [w, h] = resSel.value.split("x");   // chosen encode resolution
  ws = new WebSocket(`${proto}://${location.host}/ws?token=`
    + `${encodeURIComponent(TOKEN)}&w=${w}&h=${h}&monitor=${activeMonitor}`
    + `&vmode=${vmode}`);

  ws.onopen = () => setStatus("signaling connected, negotiating…");
  ws.onclose = () => {
    if (reconnecting) return;            // deliberate reconnect, not a drop
    setStatus("disconnected", "err");
    teardown();
  };
  ws.onerror = () => setStatus("signaling error", "err");
  ws.onmessage = async (e) => {
    const msg = JSON.parse(e.data);
    console.log("ws recv:", msg.type);
    if (msg.type === "offer") {
      await onOffer(msg.sdp);
    } else if (msg.type === "ice") {
      try {
        await pc.addIceCandidate({
          candidate: msg.candidate, sdpMLineIndex: msg.sdpMLineIndex,
        });
      } catch (err) { console.warn("addIceCandidate", err); }
    } else if (msg.type === "monitors") {
      renderMonitors(msg.list, msg.active);
    } else if (msg.type === "error") {
      setStatus("server error: " + msg.message, "err");
      showOverlay("Server error: " + msg.message);
    }
  };
}

function newPeerConnection() {
  // A public STUN server lets the browser discover its NAT-mapped address so ICE
  // can traverse the NAT between laptop and PC (the server learns the browser's
  // address via peer-reflexive candidates too). Harmless on a flat LAN/Twingate.
  pc = new RTCPeerConnection({
    iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
  });

  pc.onicecandidate = (e) => {
    if (e.candidate) {
      console.log("local ICE:", e.candidate.candidate);
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: "ice",
          candidate: e.candidate.candidate,
          sdpMLineIndex: e.candidate.sdpMLineIndex,
        }));
      }
    } else {
      console.log("local ICE gathering complete");
    }
  };

  pc.oniceconnectionstatechange = () =>
    console.log("iceConnectionState:", pc.iceConnectionState);
  pc.onicegatheringstatechange = () =>
    console.log("iceGatheringState:", pc.iceGatheringState);
  pc.onsignalingstatechange = () =>
    console.log("signalingState:", pc.signalingState);

  pc.ontrack = (e) => {
    console.log("ontrack fired: kind=" + e.track.kind);
    // Collect every track (video AND audio) into one stream -- don't let the
    // audio track (which arrives with its own stream id) replace the video.
    if (!remoteStream) {
      remoteStream = new MediaStream();
      video.srcObject = remoteStream;
    }
    remoteStream.addTrack(e.track);
    video.play().catch((err) => console.warn("video.play() rejected:", err));
    setStatus("connected", "ok");
    controlBtn.disabled = false;
    hideOverlay();
    startStats();
  };

  pc.ondatachannel = (e) => {
    if (e.channel.label === "input") {
      inputChannel = e.channel;
      inputChannel.onclose = () => { inputChannel = null; };
    }
  };

  pc.onconnectionstatechange = () => {
    console.log("connectionState:", pc.connectionState);
    if (pc.connectionState === "failed") {
      setStatus("connection failed (check Twingate / firewall UDP)", "err");
      showOverlay("WebRTC connection failed. Over Twingate, make sure UDP to " +
        "this machine is permitted for the media ports.");
    }
  };
}

async function onOffer(sdp) {
  newPeerConnection();
  await pc.setRemoteDescription({ type: "offer", sdp });
  const answer = await pc.createAnswer();
  await pc.setLocalDescription(answer);
  ws.send(JSON.stringify({ type: "answer", sdp: pc.localDescription.sdp }));
  // If the browser rejected the video m-line (answers "m=video 0"), it has no
  // decoder for this codec at all -- don't wait the whole watchdog, fall back now.
  if (/^m=video 0 /m.test(pc.localDescription.sdp || "")) {
    console.log("browser rejected the video m-line -> immediate fallback");
    if (fallbackTimer) { clearTimeout(fallbackTimer); fallbackTimer = null; }
    maybeFallback();
  }
}

function teardown() {
  controlBtn.disabled = true;
  setControlling(false);
  if (statsTimer) { clearInterval(statsTimer); statsTimer = null; }
  if (fallbackTimer) { clearTimeout(fallbackTimer); fallbackTimer = null; }
  if (pc) { pc.close(); pc = null; }
  inputChannel = null;
  remoteStream = null;
}

// Log inbound video stats so a black screen can be diagnosed: are frames
// actually arriving (bytesReceived rising) and decoding (framesDecoded rising)?
function startStats() {
  if (statsTimer) return;
  statsTimer = setInterval(async () => {
    if (!pc) return;
    const stats = await pc.getStats();
    let found = false;
    stats.forEach((r) => {
      if (r.type === "inbound-rtp" && (r.kind === "video" || r.mediaType === "video")) {
        found = true;
        console.log(`video in: recv=${r.bytesReceived}B ` +
          `framesReceived=${r.framesReceived} framesDecoded=${r.framesDecoded} ` +
          `size=${r.frameWidth}x${r.frameHeight} ` +
          `keyframes=${r.keyFramesDecoded} dropped=${r.framesDropped}`);
        setStatus(`connected · ${r.frameWidth || "?"}×${r.frameHeight || "?"} · ` +
          `${r.framesDecoded || 0} frames`, "ok");
        // Frames decoding -> this mode works; cancel the fallback watchdog.
        if ((r.framesDecoded || 0) > 0) {
          decodedOk = true;
          if (fallbackTimer) { clearTimeout(fallbackTimer); fallbackTimer = null; }
        }
      }
    });
    if (!found) console.log("stats: no inbound video report yet");
  }, 2000);
}

// ---- input forwarding ----------------------------------------------------

function sendInput(obj) {
  if (inputChannel && inputChannel.readyState === "open") {
    inputChannel.send(JSON.stringify(obj));
  }
}

// Toolbar buttons to switch which monitor is streamed (live server-side crop of
// the one desktop capture -- instant, no reconnect).
function renderMonitors(list, active) {
  monitorList = list || [];
  monitorsEl.innerHTML = "";
  if (monitorList.length <= 1) { activeMonitor = active || 0; return; }
  monitorList.forEach((m) => {
    const b = document.createElement("button");
    b.textContent = "Screen " + (m.index + 1);
    b.title = m.width + "×" + m.height;
    b.addEventListener("click", () => {
      sendInput({ t: "monitor", index: m.index });   // live crop switch
      setActiveMonitor(m.index);
    });
    monitorsEl.appendChild(b);
  });
  setActiveMonitor(active || 0);
}

function setActiveMonitor(index) {
  activeMonitor = index;
  [...monitorsEl.children].forEach((c, i) => c.classList.toggle("active", i === index));
}

// Map a client-space point to normalized [0,1] coords inside the *content* of the
// video (object-fit: contain letterboxes it).
function normalizedPoint(clientX, clientY) {
  const rect = video.getBoundingClientRect();
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return null;
  const scale = fillMode
    ? Math.max(rect.width / vw, rect.height / vh)   // cover: fill, crop edges
    : Math.min(rect.width / vw, rect.height / vh);  // contain: fit, letterbox
  const dispW = vw * scale, dispH = vh * scale;
  const offX = rect.left + (rect.width - dispW) / 2;
  const offY = rect.top + (rect.height - dispH) / 2;
  let nx = (clientX - offX) / dispW;
  let ny = (clientY - offY) / dispH;
  nx = Math.max(0, Math.min(1, nx));
  ny = Math.max(0, Math.min(1, ny));
  return { x: nx, y: ny };
}

let pendingMove = null;   // absolute position (latest wins)
function flushMove() {
  if (pendingMove) { sendInput(pendingMove); pendingMove = null; }
  if (controlling) requestAnimationFrame(flushMove);
}

function onMouseMove(e) {
  // Absolute: the remote cursor goes where the laptop pointer is, clamped to
  // the current screen so it can't run off the monitor. Switch screens with
  // the Screen buttons.
  const p = normalizedPoint(e.clientX, e.clientY);
  if (p) pendingMove = { t: "move", x: p.x, y: p.y };
}
function onMouseDown(e) {
  e.preventDefault();
  const p = normalizedPoint(e.clientX, e.clientY);
  if (p) sendInput({ t: "move", x: p.x, y: p.y });
  pressedButtons.add(e.button);
  sendInput({ t: "button", button: e.button, pressed: true });
}
function onMouseUp(e) {
  e.preventDefault();
  pressedButtons.delete(e.button);
  sendInput({ t: "button", button: e.button, pressed: false });
}

// Release everything currently held -- prevents a stuck modifier (e.g. Super)
// from making the remote unusable when the local OS steals a key-up.
function releaseAllInput() {
  pressedKeys.forEach((code) => sendInput({ t: "key", code: code, pressed: false }));
  pressedKeys.clear();
  pressedButtons.forEach((b) => sendInput({ t: "button", button: b, pressed: false }));
  pressedButtons.clear();
  sendInput({ t: "releaseall" });   // server also sweeps stuck modifiers
}
window.addEventListener("blur", releaseAllInput);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) releaseAllInput();
});
function onWheel(e) {
  e.preventDefault();
  sendInput({ t: "wheel", dx: e.deltaX, dy: e.deltaY });
}
function onContextMenu(e) { e.preventDefault(); }
function onKeyDown(e) {
  e.preventDefault();
  pressedKeys.add(e.code);
  sendInput({ t: "key", code: e.code, pressed: true });
}
function onKeyUp(e) {
  e.preventDefault();
  pressedKeys.delete(e.code);
  sendInput({ t: "key", code: e.code, pressed: false });
}

function setControlling(on) {
  if (on === controlling) return;
  controlling = on;
  controlBtn.classList.toggle("active", on);
  controlBtn.textContent = on ? "Release control (Esc·Esc)" : "Take control";
  stage.classList.toggle("controlling", on);

  if (on) {
    video.addEventListener("mousemove", onMouseMove);
    video.addEventListener("mousedown", onMouseDown);
    window.addEventListener("mouseup", onMouseUp);
    video.addEventListener("wheel", onWheel, { passive: false });
    video.addEventListener("contextmenu", onContextMenu);
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);
    requestAnimationFrame(flushMove);
  } else {
    releaseAllInput();           // never leave a key/button stuck down
    video.removeEventListener("mousemove", onMouseMove);
    video.removeEventListener("mousedown", onMouseDown);
    window.removeEventListener("mouseup", onMouseUp);
    video.removeEventListener("wheel", onWheel);
    video.removeEventListener("contextmenu", onContextMenu);
    window.removeEventListener("keydown", onKeyDown, true);
    window.removeEventListener("keyup", onKeyUp, true);
  }
}

// Double-tap Escape to release control (Escape is otherwise forwarded).
let lastEsc = 0;
window.addEventListener("keydown", (e) => {
  if (controlling && e.key === "Escape") {
    const now = performance.now();
    if (now - lastEsc < 400) {
      e.preventDefault();
      // Double-tap Esc is the escape hatch: leave fullscreen (which also
      // releases control + unlocks the keyboard), or just release control.
      if (document.fullscreenElement) document.exitFullscreen();
      else setControlling(false);
    }
    lastEsc = now;
  }
}, true);

controlBtn.addEventListener("click", () => setControlling(!controlling));
fillBtn.addEventListener("click", () => {
  fillMode = !fillMode;
  video.classList.toggle("fill", fillMode);
  fillBtn.classList.toggle("active", fillMode);
});
soundBtn.addEventListener("click", () => {
  video.muted = !video.muted;
  soundBtn.textContent = video.muted ? "🔇 Sound off" : "🔊 Sound on";
  soundBtn.classList.toggle("active", !video.muted);
  if (!video.muted) video.play().catch(() => {});
});
resSel.addEventListener("change", () => reconnect("changing resolution…"));
bitrateSel.addEventListener("change", () => {
  sendInput({ t: "bitrate", kbps: Number(bitrateSel.value) });
});

// ---- zoom / pan (read small text, esp. from a phone) ---------------------
// Client-side CSS zoom of the received video: reveals the real 1440p detail,
// no server/bandwidth change. Input mapping still works (it reads the
// transformed bounding rect). Pinch + drag on touch, buttons on desktop.
function clampPan() {
  if (zoom <= 1) { panX = 0; panY = 0; return; }
  const sw = video.offsetWidth, sh = video.offsetHeight;
  panX = Math.max(sw - sw * zoom, Math.min(0, panX));
  panY = Math.max(sh - sh * zoom, Math.min(0, panY));
}
function applyTransform() {
  clampPan();
  video.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;
}
function zoomAt(factor, fx, fy) {
  const nz = Math.max(1, Math.min(6, zoom * factor));
  if (nz === zoom) return;
  panX = fx - (fx - panX) * (nz / zoom);   // keep the focal point stationary
  panY = fy - (fy - panY) * (nz / zoom);
  zoom = nz;
  applyTransform();
}
const center = () => [video.offsetWidth / 2, video.offsetHeight / 2];
zoomInBtn.addEventListener("click", () => zoomAt(1.4, ...center()));
zoomOutBtn.addEventListener("click", () => zoomAt(1 / 1.4, ...center()));
zoomResetBtn.addEventListener("click", () => {
  zoom = 1; panX = 0; panY = 0; applyTransform();
});

let pinchDist = 0, lastTouch = null;
const touchDist = (t) =>
  Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
stage.addEventListener("touchstart", (e) => {
  if (e.touches.length === 2) pinchDist = touchDist(e.touches);
  else if (e.touches.length === 1 && !controlling && zoom > 1)
    lastTouch = { x: e.touches[0].clientX, y: e.touches[0].clientY };
}, { passive: false });
stage.addEventListener("touchmove", (e) => {
  const sr = stage.getBoundingClientRect();
  if (e.touches.length === 2) {
    e.preventDefault();
    const d = touchDist(e.touches);
    if (pinchDist) {
      const mx = (e.touches[0].clientX + e.touches[1].clientX) / 2 - sr.left;
      const my = (e.touches[0].clientY + e.touches[1].clientY) / 2 - sr.top;
      zoomAt(d / pinchDist, mx, my);
    }
    pinchDist = d;
  } else if (e.touches.length === 1 && lastTouch && !controlling && zoom > 1) {
    e.preventDefault();
    const t = e.touches[0];
    panX += t.clientX - lastTouch.x; panY += t.clientY - lastTouch.y;
    lastTouch = { x: t.clientX, y: t.clientY };
    applyTransform();
  }
}, { passive: false });
stage.addEventListener("touchend", () => { pinchDist = 0; lastTouch = null; });

// Mouse drag to pan when zoomed and not controlling (desktop).
let panDrag = null;
video.addEventListener("mousedown", (e) => {
  if (!controlling && zoom > 1) { panDrag = { x: e.clientX, y: e.clientY }; e.preventDefault(); }
});
window.addEventListener("mousemove", (e) => {
  if (!panDrag) return;
  panX += e.clientX - panDrag.x; panY += e.clientY - panDrag.y;
  panDrag = { x: e.clientX, y: e.clientY }; applyTransform();
});
window.addEventListener("mouseup", () => { panDrag = null; });

// ---- on-screen / mobile keyboard ----------------------------------------
// Phones won't show a soft keyboard for a <video>. The Keyboard button focuses a
// hidden field; physical keyboards + named keys (Enter/Backspace/arrows) arrive via
// keydown with a real .code, while mobile IME characters arrive via 'input' -- we
// diff the field value and map each character to keystrokes (assumes a US host
// layout, since the server injects physical keycodes).
let kbdLast = "";
const CHAR_MAP = (() => {
  const m = {};
  for (const c of "abcdefghijklmnopqrstuvwxyz") m[c] = ["Key" + c.toUpperCase(), false];
  for (const c of "ABCDEFGHIJKLMNOPQRSTUVWXYZ") m[c] = ["Key" + c, true];
  for (const c of "0123456789") m[c] = ["Digit" + c, false];
  Object.assign(m, {
    " ": ["Space", false], "\t": ["Tab", false], "\n": ["Enter", false],
    "!": ["Digit1", true], "@": ["Digit2", true], "#": ["Digit3", true],
    "$": ["Digit4", true], "%": ["Digit5", true], "^": ["Digit6", true],
    "&": ["Digit7", true], "*": ["Digit8", true], "(": ["Digit9", true],
    ")": ["Digit0", true],
    "-": ["Minus", false], "_": ["Minus", true],
    "=": ["Equal", false], "+": ["Equal", true],
    "[": ["BracketLeft", false], "{": ["BracketLeft", true],
    "]": ["BracketRight", false], "}": ["BracketRight", true],
    "\\": ["Backslash", false], "|": ["Backslash", true],
    ";": ["Semicolon", false], ":": ["Semicolon", true],
    "'": ["Quote", false], '"': ["Quote", true],
    ",": ["Comma", false], "<": ["Comma", true],
    ".": ["Period", false], ">": ["Period", true],
    "/": ["Slash", false], "?": ["Slash", true],
    "`": ["Backquote", false], "~": ["Backquote", true],
  });
  return m;
})();

function tapKey(code, shift) {
  if (shift) sendInput({ t: "key", code: "ShiftLeft", pressed: true });
  sendInput({ t: "key", code, pressed: true });
  sendInput({ t: "key", code, pressed: false });
  if (shift) sendInput({ t: "key", code: "ShiftLeft", pressed: false });
}
function sendChar(ch) {
  const m = CHAR_MAP[ch];
  if (m) tapKey(m[0], m[1]);
}

// Physical/named keys forward directly. Skip IME composition (keyCode 229 / empty
// code) -- those characters come through the 'input' handler below instead.
kbdInput.addEventListener("keydown", (e) => {
  if (e.isComposing || e.keyCode === 229 || !e.code) return;
  e.preventDefault(); e.stopPropagation();
  pressedKeys.add(e.code);
  sendInput({ t: "key", code: e.code, pressed: true });
});
kbdInput.addEventListener("keyup", (e) => {
  if (!e.code) return;
  e.preventDefault(); e.stopPropagation();
  pressedKeys.delete(e.code);
  sendInput({ t: "key", code: e.code, pressed: false });
});

// Mobile characters: diff the field value vs last to find additions/deletions.
kbdInput.addEventListener("input", () => {
  const v = kbdInput.value;
  let i = 0;
  while (i < v.length && i < kbdLast.length && v[i] === kbdLast[i]) i++;
  for (let k = kbdLast.length - i; k > 0; k--) tapKey("Backspace", false);
  for (const ch of v.slice(i)) sendChar(ch);
  kbdLast = v;
  if (v.length > 500) { kbdInput.value = ""; kbdLast = ""; }   // don't grow forever
});

function toggleKeyboard() {
  if (document.activeElement === kbdInput) {
    kbdInput.blur();
  } else {
    kbdInput.value = ""; kbdLast = "";
    kbdInput.focus();   // must run inside this click gesture to pop the soft keyboard
    controls.classList.remove("open");   // close the menu so it doesn't cover the view
  }
}
keyboardBtn.addEventListener("click", toggleKeyboard);
kbdInput.addEventListener("focus", () => keyboardBtn.classList.add("active"));
kbdInput.addEventListener("blur", () => {
  keyboardBtn.classList.remove("active");
  kbdInput.value = ""; kbdLast = "";
});

// ---- collapsing toolbar menu (narrow / phone screens) -------------------
menuBtn.addEventListener("click", (e) => {
  e.stopPropagation();           // don't let the outside-close handler fire on this
  controls.classList.toggle("open");
});
document.addEventListener("click", (e) => {
  if (controls.classList.contains("open")
      && !controls.contains(e.target) && e.target !== menuBtn) {
    controls.classList.remove("open");   // tap anywhere else closes it
  }
});

fullscreenBtn.addEventListener("click", () => {
  if (!document.fullscreenElement) stage.requestFullscreen();
  else document.exitFullscreen();
});

// Keyboard Lock (Chrome/Edge) captures OS-level shortcuts -- Super/Win key
// combos, Alt+Tab, Ctrl+W -- so they reach the remote instead of the laptop.
// It only works in fullscreen, so we tie it to the fullscreen state.
function lockKeyboard() {
  if (navigator.keyboard && navigator.keyboard.lock) {
    navigator.keyboard.lock().catch((e) => console.warn("keyboard lock:", e));
  }
}
function unlockKeyboard() {
  if (navigator.keyboard && navigator.keyboard.unlock) {
    try { navigator.keyboard.unlock(); } catch (e) { /* ignore */ }
  }
}
document.addEventListener("fullscreenchange", () => {
  if (document.fullscreenElement) {
    setControlling(true);     // entering fullscreen takes control
    lockKeyboard();
  } else {
    unlockKeyboard();
    setControlling(false);    // leaving fullscreen releases it
  }
});

// ---- start ---------------------------------------------------------------

if (!TOKEN) {
  setStatus("missing ?token= in URL", "err");
  showOverlay("This URL is missing its access token. Use the full link printed " +
    "by the server (http://…/?token=…).");
} else {
  showOverlay("Connecting…");
  connect();
}
