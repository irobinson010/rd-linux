"use strict";

console.log("rdclient build 29 loaded");

const params = new URLSearchParams(location.search);
// The token arrives once via ?token=… then lives in localStorage; scrub it from
// the address bar immediately so it never sits in history / bookmarks / synced
// tabs / screenshots. (It still rides the ws URL query, which is never displayed,
// and the server's access log records paths only.) No token anywhere -> the
// token prompt below lets you type it in, so a device can be bootstrapped
// without the secret ever touching a URL.
let TOKEN = "";
try {
  const urlToken = params.get("token");
  if (urlToken) {
    TOKEN = urlToken;
    localStorage.setItem("rdtoken", urlToken);
    params.delete("token");
    const qs = params.toString();
    history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
  } else {
    TOKEN = localStorage.getItem("rdtoken") || "";
  }
} catch (e) {   // storage blocked (strict privacy mode) -> old URL behaviour
  TOKEN = params.get("token") || "";
}

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
const sharebtn = document.getElementById("sharebtn");
const sharebox = document.getElementById("sharebox");
const shareLink = document.getElementById("sharelink");
const shareMsg = document.getElementById("sharemsg");
const sharecopy = document.getElementById("sharecopy");
const sharerevoke = document.getElementById("sharerevoke");
const shareclose = document.getElementById("shareclose");
const clipbtn = document.getElementById("clipbtn");
const clipbox = document.getElementById("clipbox");
const clipText = document.getElementById("cliptext");
const clipsend = document.getElementById("clipsend");
const clipclose = document.getElementById("clipclose");
const tokenbox = document.getElementById("tokenbox");
const tokenMsg = document.getElementById("tokenmsg");
const tokenInput = document.getElementById("tokeninput");
const tokengo = document.getElementById("tokengo");
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
let canControl = true;       // set false by the server's "role" msg for view-only
let statsTimer = null;
let remoteStream = null;     // one stream holding the video + audio tracks
let monitorList = [];        // [{index,width,height}, ...] from the server
let activeMonitor = 0;
const pressedKeys = new Set();     // codes sent down but not yet up
const pressedButtons = new Set();
let clipboardEnabled = false;      // server advertises it in the "role" message
let lastClip = "";                 // last text synced EITHER way (echo guard)

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

  let opened = false;   // did the upgrade succeed? (a 403 closes before onopen)
  ws.onopen = () => { opened = true; setStatus("signaling connected, negotiating…"); };
  ws.onclose = (e) => {
    if (reconnecting) return;            // deliberate reconnect, not a drop
    teardown();
    if (e.code === 4001) {               // controller revoked view access
      setStatus("view access revoked", "err");
      showOverlay("The controller revoked view access.");
    } else if (!opened) {
      // Rejected before the upgrade: bad/expired token (or server unreachable).
      // Re-prompt rather than dead-ending -- the stored token may be stale.
      setStatus("not authorized", "err");
      showTokenPrompt("Couldn't connect: the access token was rejected (or the " +
        "server is unreachable). Enter the current token to try again.");
    } else {
      setStatus("disconnected", "err");
    }
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
    } else if (msg.type === "role") {
      applyRole(msg.control);
      clipboardEnabled = !!msg.clipboard;
      updateClipUI();
    } else if (msg.type === "clipboard") {
      onRemoteClipboard(msg.text);
    } else if (msg.type === "view_link") {
      showShare(location.origin + "/?token=" + encodeURIComponent(msg.token),
        msg.ttl_s);
    } else if (msg.type === "view_revoked") {
      sharebox.classList.add("hidden");
      setStatus("revoked " + msg.tokens + " view link(s), disconnected "
        + msg.viewers + " viewer(s)", "ok");
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
    controlBtn.disabled = !canControl;     // view-only sessions can't take control
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

// Signaling-channel send (offer/answer/ice go over the same socket). Used for
// control messages that aren't input events -- e.g. clipboard sync.
function wsSend(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// ---- shared clipboard ----------------------------------------------------
// Controller-only. Two directions, both echo-guarded by `lastClip`:
//   remote -> local: server pushes {clipboard,text}; we write it to the local
//                    clipboard (best-effort) and show it in the panel.
//   local -> remote: on focus / take-control / panel we read the local clipboard
//                    and send it, so a subsequent Ctrl+V in the remote pastes it.
// Reading the local clipboard silently needs permission (Chrome/Edge grant it on
// a gesture); where it's blocked, the panel textarea is the universal fallback.
function updateClipUI() {
  clipbtn.style.display = (clipboardEnabled && canControl) ? "" : "none";
}

function onRemoteClipboard(text) {
  if (typeof text !== "string") return;
  lastClip = text;
  clipText.value = text;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => { /* panel still shows it */ });
  }
}

function pushLocalClipboard() {
  if (!clipboardEnabled || !canControl) return;
  if (!navigator.clipboard || !navigator.clipboard.readText) return;
  navigator.clipboard.readText().then((t) => {
    if (t && t !== lastClip) {
      lastClip = t;
      clipText.value = t;
      wsSend({ type: "clipboard", text: t });
    }
  }).catch(() => { /* blocked/denied -> use the panel's Send to remote */ });
}

clipbtn.addEventListener("click", () => {
  clipbox.classList.remove("hidden");
  controls.classList.remove("open");
  pushLocalClipboard();            // opportunistically pull the local clipboard
  clipText.focus();
});
clipsend.addEventListener("click", () => {
  lastClip = clipText.value;
  wsSend({ type: "clipboard", text: clipText.value });
  clipbox.classList.add("hidden");
  setStatus("clipboard sent to remote", "ok");
});
clipclose.addEventListener("click", () => clipbox.classList.add("hidden"));
// When the tab regains focus the user may have just copied something locally.
window.addEventListener("focus", pushLocalClipboard);

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

// The server tells us our role; view-only sessions lose the control + keyboard UI.
function applyRole(control) {
  canControl = control;
  if (!control) {
    setControlling(false);
    controlBtn.textContent = "👁 View only";
    controlBtn.disabled = true;
    controlBtn.classList.remove("active");
    keyboardBtn.style.display = "none";   // typing does nothing for a viewer
    sharebtn.style.display = "none";      // viewers can't mint links
    clipbtn.style.display = "none";       // clipboard is controller-only
  }
}

// ---- share a view-only link (control session only) ----------------------
sharebtn.addEventListener("click", () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "make_view_link" }));
  }
});
function showShare(url, ttlS) {
  shareMsg.textContent = "View-only link — recipients can watch but not control. "
    + (ttlS ? "Expires in ~" + Math.round(ttlS / 3600) + " h."
            : "Valid until the server restarts.");
  shareLink.value = url;
  sharebox.classList.remove("hidden");
  shareLink.focus(); shareLink.select();
  if (navigator.clipboard) {
    navigator.clipboard.writeText(url)
      .then(() => setStatus("view link copied to clipboard", "ok"), () => {});
  }
}
sharecopy.addEventListener("click", () => {
  shareLink.select();
  if (navigator.clipboard) navigator.clipboard.writeText(shareLink.value);
  else { try { document.execCommand("copy"); } catch (e) { /* */ } }
});
sharerevoke.addEventListener("click", () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "revoke_view_links" }));
  }
});
shareclose.addEventListener("click", () => sharebox.classList.add("hidden"));

function setControlling(on) {
  if (on && !canControl) return;          // view-only sessions can't control
  if (on === controlling) return;
  controlling = on;
  controlBtn.classList.toggle("active", on);
  if (canControl) controlBtn.textContent = on ? "Release control (Esc·Esc)" : "Take control";
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
    pushLocalClipboard();        // sync local clipboard in so Ctrl+V works now
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

controlBtn.addEventListener("click", () => {
  setControlling(!controlling);
  controls.classList.remove("open");   // close the menu so touch drives the remote
});
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
  if (controlling) return;            // controlling -> touch-to-control handles it
  if (e.touches.length === 2) pinchDist = touchDist(e.touches);
  else if (e.touches.length === 1 && !controlling && zoom > 1)
    lastTouch = { x: e.touches[0].clientX, y: e.touches[0].clientY };
}, { passive: false });
stage.addEventListener("touchmove", (e) => {
  if (controlling) return;            // controlling -> touch-to-control handles it
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

// ---- touch-to-control: drive the cursor by touch while controlling -------
// 1 finger: tap = left click, double-tap = double click, drag = move cursor.
// 2 fingers: tap = right click, drag = scroll. (Pinch-zoom is for view mode.)
let gMax = 0, gStart = null, gMoved = false;      // one touch gesture's state
let gTwoY = 0, gTwoMoved = false, lastTap = 0;
const TAP_MS = 300, TAP_SLOP = 12, SCROLL_STEP = 14;
const midXY = (t) => ({ x: (t[0].clientX + t[1].clientX) / 2,
                        y: (t[0].clientY + t[1].clientY) / 2 });

stage.addEventListener("touchstart", (e) => {
  if (!controlling) return;
  e.preventDefault();
  if (gMax === 0 && e.touches.length === 1) {     // first finger of a new gesture
    const t = e.touches[0];
    gStart = { x: t.clientX, y: t.clientY, time: performance.now() };
    gMoved = false;
    const p = normalizedPoint(t.clientX, t.clientY);
    if (p) sendInput({ t: "move", x: p.x, y: p.y });   // cursor jumps to the finger
  }
  gMax = Math.max(gMax, e.touches.length);
  if (e.touches.length === 2) {
    const m = midXY(e.touches);
    gTwoY = m.y; gTwoMoved = false;
    const p = normalizedPoint(m.x, m.y);
    if (p) sendInput({ t: "move", x: p.x, y: p.y });   // place cursor for right-click
  }
}, { passive: false });

stage.addEventListener("touchmove", (e) => {
  if (!controlling) return;
  e.preventDefault();
  if (e.touches.length === 1) {
    const t = e.touches[0];
    const p = normalizedPoint(t.clientX, t.clientY);
    if (p) sendInput({ t: "move", x: p.x, y: p.y });   // cursor follows the finger
    if (gStart && Math.hypot(t.clientX - gStart.x, t.clientY - gStart.y) > TAP_SLOP)
      gMoved = true;
  } else if (e.touches.length === 2) {
    const y = midXY(e.touches).y;
    if (Math.abs(y - gTwoY) >= SCROLL_STEP) {
      sendInput({ t: "wheel", dx: 0, dy: y - gTwoY });  // (flip the sign if reversed)
      gTwoY = y; gTwoMoved = true;
    }
  }
}, { passive: false });

stage.addEventListener("touchend", (e) => {
  if (!controlling) return;
  e.preventDefault();
  if (e.touches.length > 0) return;        // wait until ALL fingers are lifted
  const now = performance.now();
  if (gMax === 1 && gStart && !gMoved && now - gStart.time < TAP_MS) {
    sendInput({ t: "button", button: 0, pressed: true });   // tap -> left click
    sendInput({ t: "button", button: 0, pressed: false });
    if (now - lastTap < 350) {                              // double-tap -> dbl click
      sendInput({ t: "button", button: 0, pressed: true });
      sendInput({ t: "button", button: 0, pressed: false });
    }
    lastTap = now;
  } else if (gMax === 2 && !gTwoMoved) {                    // 2-finger tap -> right click
    sendInput({ t: "button", button: 2, pressed: true });
    sendInput({ t: "button", button: 2, pressed: false });
  }
  gMax = 0; gStart = null; gMoved = false; gTwoMoved = false;
}, { passive: false });

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

// TEMP on-screen keyboard-event debug (remove once Enter is sorted).
const kbddbg = document.getElementById("kbddbg");
let dbgLines = [];
function dbg(s) {
  dbgLines.unshift(s);
  dbgLines = dbgLines.slice(0, 8);
  kbddbg.textContent = dbgLines.join("\n");
  kbddbg.classList.remove("hidden");
}

// Physical/named keys forward directly. Skip IME composition (keyCode 229 / empty
// code) -- those characters come through the 'input' handler below instead.
kbdInput.addEventListener("keydown", (e) => {
  dbg("kd code=" + (e.code || "none") + " key=" + e.key + " kc=" + e.keyCode + " comp=" + e.isComposing);
  if (e.isComposing || e.keyCode === 229 || !e.code) return;
  e.preventDefault(); e.stopPropagation();
  pressedKeys.add(e.code);
  sendInput({ t: "key", code: e.code, pressed: true });
});
kbdInput.addEventListener("keyup", (e) => {
  dbg("ku code=" + (e.code || "none") + " key=" + e.key);
  if (!e.code) return;
  e.preventDefault(); e.stopPropagation();
  pressedKeys.delete(e.code);
  sendInput({ t: "key", code: e.code, pressed: false });
});

// Mobile soft keyboards: 'beforeinput' reports the intent (text, line break,
// backspace) reliably even when keydown gives no .code and the return key inserts
// no newline. We act on it and preventDefault so the hidden field stays empty.
kbdInput.addEventListener("input", (e) =>
  dbg("in it=" + e.inputType + " data=" + JSON.stringify(e.data)));
kbdInput.addEventListener("beforeinput", (e) => {
  dbg("bi it=" + e.inputType + " data=" + JSON.stringify(e.data));
  const it = e.inputType;
  if (it === "insertText" && e.data != null) {
    for (const ch of e.data) sendChar(ch);
  } else if (it === "insertLineBreak" || it === "insertParagraph") {
    tapKey("Enter", false);
  } else if (it === "deleteContentBackward") {
    tapKey("Backspace", false);
  } else if (it === "deleteContentForward") {
    tapKey("Delete", false);
  } else {
    return;   // composition in progress / other -> leave it (no preventDefault)
  }
  e.preventDefault();
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
  dbg("blur");
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

// ---- token prompt ---------------------------------------------------------
// Shown when no token is known (fresh device, no ?token= link) or the stored
// one was rejected. Lets you bootstrap by typing the token instead of pasting
// a secret-bearing URL.
function showTokenPrompt(msg) {
  hideOverlay();
  tokenMsg.textContent = msg;
  tokenbox.classList.remove("hidden");
  tokenInput.value = "";
  tokenInput.focus();
}
function submitToken() {
  const t = tokenInput.value.trim();
  if (!t) return;
  TOKEN = t;
  try { localStorage.setItem("rdtoken", t); } catch (e) { /* */ }
  tokenbox.classList.add("hidden");
  showOverlay("Connecting…");
  connect();
}
tokengo.addEventListener("click", submitToken);
tokenInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") submitToken();
});

// ---- start ---------------------------------------------------------------

if (!TOKEN) {
  setStatus("no access token", "err");
  showTokenPrompt("Enter this machine's access token (printed by the server at "
    + "startup; stored in ~/.config/rdserver/rd.env).");
} else {
  showOverlay("Connecting…");
  connect();
}
