/* UAP Finder node setup page. No framework, no build step; text is set with textContent only. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  let state = { csrf: "", password_set: false, authenticated: false, enrolled: false };
  let lastStatus = null;

  function cookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }

  async function api(path, body) {
    const opts = { credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-CSRF-Token"] = cookie("uapf_csrf") || state.csrf;
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(path, opts);
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) {
      const err = new Error((data && data.error) || "Request failed (" + r.status + ")");
      err.status = r.status;
      throw err;
    }
    return data;
  }

  function banner(text, kind) {
    const b = $("banner");
    b.textContent = text;
    b.className = "banner" + (kind ? " " + kind : "");
    b.hidden = !text;
  }

  function busy(form, on) {
    form.querySelectorAll("button").forEach((b) => { b.disabled = on; });
  }

  // --- state and status --------------------------------------------------------------

  async function loadState() {
    state = await api("/api/state");
    $("hostname").textContent = state.hostname + ".local";
    $("version").textContent = "Software " + state.version;
    const locked = state.password_set && !state.authenticated;
    $("login").hidden = !locked;
    $("logout").hidden = !(state.password_set && state.authenticated);
    $("setup").hidden = state.enrolled && state.password_set;
    $("step-claim").classList.toggle("done", state.enrolled);
    $("enrolled-as").hidden = !state.enrolled;
    if (state.enrolled) $("enrolled-as").textContent = "Enrolled as node " + state.node_id + ".";
    $("step-pw").classList.toggle("done", state.password_set);
    $("pw-current").hidden = $("pw-current-label").hidden = !state.password_set;
    $("pw-current").required = state.password_set;
    if (state.enroll_url && !$("enroll-url").value) $("enroll-url").value = state.enroll_url;
    document.querySelectorAll("#setup form, #wifi-form, #settings-form, [data-action], #wifi-scan").forEach((el) => {
      el.toggleAttribute("inert", locked);
    });
  }

  function dot(kind) {
    const d = document.createElement("span");
    d.className = "dot " + kind;
    d.setAttribute("aria-hidden", "true");
    return d;
  }

  function item(label, value, kind) {
    const div = document.createElement("div");
    const dt = document.createElement("dt");
    dt.appendChild(dot(kind || ""));
    dt.appendChild(document.createTextNode(label));
    const dd = document.createElement("dd");
    dd.textContent = value;
    div.appendChild(dt);
    div.appendChild(dd);
    if (kind) div.setAttribute("aria-label", label + ": " + value + " (" + ({ ok: "good", warn: "attention", err: "problem" }[kind] || "") + ")");
    return div;
  }

  function fmtAge(days) {
    if (days === null || days === undefined) return "";
    if (days < 1) return Math.round(days * 24) + " h ago";
    return days.toFixed(1) + " days ago";
  }

  function renderStatus(s) {
    lastStatus = s;
    const hw = s.hardware || {};
    const list = $("status");
    list.replaceChildren();
    const cam = hw.camera || {};
    const camText = cam.kind === "csi" ? "CSI camera " + (cam.sensor || "").toUpperCase() + " (CAM" + (cam.sensor_id || 0) + ")"
      : cam.kind === "uvc" ? "USB camera " + (cam.name || cam.device || "") : "Not found";
    const node = s.node || {};
    const camOpen = node.camera && (node.camera.opened || node.camera.preview_running);
    list.appendChild(item("Camera", camText, cam.kind && cam.kind !== "none" ? (camOpen || node.mode === "waiting" ? "ok" : "warn") : "err"));

    const g = s.gps || {};
    let gpsText = "gpsd not running";
    let gpsKind = "err";
    if (g.gpsd) {
      const fix = g.mode >= 3 ? "3-D fix" : g.mode === 2 ? "2-D fix" : "no fix yet";
      const sats = g.satellites_used !== undefined && g.satellites_used !== null ? ", " + g.satellites_used + " satellites used" +
        (g.satellites_visible ? " of " + g.satellites_visible : "") : "";
      gpsText = fix + sats;
      gpsKind = g.mode >= 3 ? "ok" : g.mode === 2 ? "warn" : "warn";
      if (g.mode >= 2 && typeof g.lat === "number") gpsText += " (" + g.lat.toFixed(4) + ", " + g.lon.toFixed(4) + ")";
    }
    list.appendChild(item("GPS", gpsText, gpsKind));

    const t = s.time || {};
    const pps = hw.pps || {};
    let timeText = "chrony not available";
    let timeKind = "err";
    if (t.available) {
      const err = typeof t.error_ms === "number" ? t.error_ms : null;
      timeText = (err !== null ? "error ≈ " + (err < 1 ? (err * 1000).toFixed(0) + " µs" : err.toFixed(1) + " ms") : "unknown") +
        ", source " + (t.clock_ref || "?") + (t.pps_locked ? ", PPS locked" : pps.device ? ", PPS not locked yet" : ", no PPS");
      timeKind = err !== null && err <= 2 ? "ok" : "warn";
    }
    list.appendChild(item("Time", timeText, timeKind));

    const imu = hw.imu || {};
    const att = node.attitude || {};
    let oriText = imu.kind === "bno055" ? "BNO055 on I2C bus " + imu.bus : "No orientation sensor (optional)";
    if (imu.kind === "bno055" && typeof att.pitch_deg === "number") oriText += ": camera " + att.pitch_deg.toFixed(0) + "° up";
    list.appendChild(item("Orientation", oriText, imu.kind === "bno055" ? "ok" : ""));

    const cfg = s.config || {};
    const adsbText = cfg.adsb_source === "dump1090" ? "Local receiver (readsb)" :
      cfg.adsb_source === "adsbx_v2" ? "Online feed " + (cfg.adsb_host || "") : "Off";
    list.appendChild(item("Aircraft (ADS-B)", adsbText, cfg.adsb_source && cfg.adsb_source !== "none" ? "ok" : "warn"));

    const st = s.storage || {};
    list.appendChild(item("Storage", (st.nvme ? "NVMe SSD" : "microSD card") + (st.free_gb !== undefined ? ", " + st.free_gb + " GB free of " + st.total_gb : ""),
      st.free_gb !== undefined && st.free_gb < 2 ? "err" : st.nvme ? "ok" : "warn"));

    const srv = s.server || {};
    let srvText = s.enrolled ? "Enrolled as " + (s.identity && s.identity.node_id) : "Not enrolled";
    let srvKind = s.enrolled ? "warn" : "err";
    if (s.enrolled && srv.checked) {
      if (srv.online) { srvText += ", the server hears this node"; srvKind = "ok"; }
      else if (srv.reachable) srvText += ", server reachable, no heartbeat seen yet";
      else srvText += ", server not reachable";
    }
    list.appendChild(item("Server", srvText, srvKind));

    const cal = s.calibration;
    let calText = "Waiting for a clear night";
    if (cal) calText = "From the stars, " + fmtAge(cal.age_days) + (cal.rms_deg ? " (±" + Number(cal.rms_deg).toFixed(2) + "°, " + cal.stars_matched + " stars)" : "");
    else if (node.pointing_source === "imu") calText += " (using the orientation sensor until then)";
    const last = s.calibration_last_attempt;
    if (!cal && last && last.attempted && last.reason) calText += ". Last try: " + last.reason;
    list.appendChild(item("Pointing", calText, cal ? "ok" : "warn"));

    const net = s.network || {};
    list.appendChild(item("Network", net.hotspot ? "Hotspot (no other network)" : net.connected ? "Connected" : net.manager ? "Not connected" : "Unknown",
      net.connected ? "ok" : "warn"));
    list.appendChild(item("Software", s.version + (node.running ? ", node " + node.mode : ", node not running"), node.running ? "ok" : "warn"));

    const w = $("warnings");
    w.replaceChildren();
    (s.warnings || []).forEach((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      w.appendChild(li);
    });
    $("updated").textContent = "Updated " + new Date().toLocaleTimeString();
    $("net-summary").textContent = net.connected ? "Connected." : net.hotspot ? "Only the node's own hotspot is active: connect Ethernet or choose a Wi-Fi network below." : "No network yet.";
    $("step-net").classList.toggle("done", !!net.connected);
    const devs = $("net-devices");
    devs.replaceChildren();
    (net.devices || []).forEach((d) => {
      const li = document.createElement("li");
      li.textContent = (d.type === "wifi" ? "Wi-Fi " : "Ethernet ") + d.device + ": " + d.state + (d.connection ? " (" + d.connection + ")" : "");
      devs.appendChild(li);
    });
    drawOverlay();
  }

  async function refresh() {
    try { renderStatus(await api("/api/status")); } catch (e) { $("updated").textContent = "Status unavailable: " + e.message; }
  }

  // --- preview -----------------------------------------------------------------------

  let previewOn = false;
  let previewTimer = null;
  let previewUntil = 0;
  let previewUrl = null;

  function setPreview(on) {
    previewOn = on;
    $("preview-toggle").textContent = on ? "Stop preview" : "Start preview";
    $("preview-toggle").setAttribute("aria-pressed", String(on));
    if (on) { previewUntil = Date.now() + 5 * 60 * 1000; tick(); }
    else { clearTimeout(previewTimer); }
  }

  async function tick() {
    if (!previewOn) return;
    if (Date.now() > previewUntil) { setPreview(false); return; }
    try {
      const r = await fetch("/api/preview.jpg?t=" + Date.now(), { credentials: "same-origin" });
      if (r.ok) {
        const blob = await r.blob();
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = URL.createObjectURL(blob);
        const img = $("preview-img");
        img.onload = drawOverlay;
        img.src = previewUrl;
        img.hidden = false;
        $("preview-msg").hidden = true;
      } else {
        $("preview-msg").hidden = false;
        $("preview-msg").textContent = "Waiting for the camera…";
      }
    } catch (e) { /* keep trying */ }
    previewTimer = setTimeout(tick, 1000);
  }

  // Elevation lines from the camera attitude (pinhole model; enough for aiming).
  function drawOverlay() {
    const canvas = $("preview-overlay");
    const box = $("preview-box");
    const w = box.clientWidth, h = box.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const s = lastStatus;
    const node = (s && s.node) || {};
    const cam = node.camera || {};
    const att = node.attitude || {};
    const known = node.pointing_source && node.pointing_source !== "prior" && typeof att.pitch_deg === "number";
    const aim = $("aim");
    if (!known) {
      aim.textContent = "Elevation lines appear once the orientation sensor or the star calibration knows where the camera points. " +
        "Until then: aim at open sky, 45° up or more.";
      return;
    }
    aim.textContent = "Camera points " + att.pitch_deg.toFixed(0) + "° above the horizon" +
      (att.pitch_deg < 45 ? ": tilt it further up (45° to 90°)." : ".");
    const W = cam.width || 1920, H = cam.height || 1080;
    const scale = Math.min(w / W, h / H);
    const ox = (w - W * scale) / 2, oy = (h - H * scale) / 2;
    const f = (W / 2) / Math.tan(((cam.fov_h_deg || 90) * Math.PI) / 360);
    const rad = Math.PI / 180;
    const yaw = (att.yaw_deg || 0) * rad, p = att.pitch_deg * rad, roll = (att.roll_deg || 0) * rad;
    const fw = [Math.cos(p) * Math.sin(yaw), Math.cos(p) * Math.cos(yaw), Math.sin(p)];
    const r0 = [Math.cos(yaw), -Math.sin(yaw), 0];
    const d0 = [fw[1] * r0[2] - fw[2] * r0[1], fw[2] * r0[0] - fw[0] * r0[2], fw[0] * r0[1] - fw[1] * r0[0]];
    const rt = r0.map((v, i) => Math.cos(roll) * v + Math.sin(roll) * d0[i]);
    const dn = d0.map((v, i) => Math.cos(roll) * v - Math.sin(roll) * r0[i]);
    const dot3 = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    function project(az, el) {
      const v = [Math.sin(az) * Math.cos(el), Math.cos(az) * Math.cos(el), Math.sin(el)];
      const z = dot3(v, fw);
      if (z < 0.05) return null;
      return [ox + (W / 2 + (f * dot3(v, rt)) / z) * scale, oy + (H / 2 + (f * dot3(v, dn)) / z) * scale];
    }
    ctx.font = "13px system-ui, sans-serif";
    ctx.lineWidth = 1.5;
    [0, 15, 30, 45, 60, 75].forEach((e) => {
      ctx.strokeStyle = e === 0 ? "rgba(255,154,154,0.9)" : "rgba(143,208,255,0.6)";
      ctx.fillStyle = ctx.strokeStyle;
      ctx.beginPath();
      let started = false, label = null;
      for (let a = 0; a <= 360; a += 2) {
        const pt = project(a * rad, e * rad);
        if (!pt) { started = false; continue; }
        if (!started) { ctx.moveTo(pt[0], pt[1]); started = true; } else ctx.lineTo(pt[0], pt[1]);
        if (pt[0] > ox + 4 && pt[0] < ox + W * scale - 40 && pt[1] > oy + 12 && pt[1] < oy + H * scale - 4 && (!label || pt[0] < label[0])) label = pt;
      }
      ctx.stroke();
      if (label) ctx.fillText(e === 0 ? "horizon" : e + "°", label[0] + 4, label[1] - 4);
    });
    const z = project(0, Math.PI / 2);
    if (z) { ctx.fillStyle = "#ffd27a"; ctx.beginPath(); ctx.arc(z[0], z[1], 4, 0, 2 * Math.PI); ctx.fill(); ctx.fillText("straight up", z[0] + 6, z[1] - 6); }
  }

  // --- forms ------------------------------------------------------------------------------

  function onSubmit(id, fn) {
    $(id).addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const form = ev.currentTarget;
      busy(form, true);
      try { await fn(form); } catch (e) { banner(e.message, "err"); } finally { busy(form, false); }
    });
  }

  onSubmit("login-form", async () => {
    await api("/api/login", { password: $("login-pw").value });
    $("login-pw").value = "";
    banner("Logged in.", "ok");
    await loadState();
  });

  onSubmit("enroll-form", async () => {
    const res = await api("/api/enroll", { code: $("claim").value });
    banner("Enrolled as node " + res.node_id + ". The node restarts its software and appears on the map within a few minutes" +
      (res.tailscale ? " (after joining the private network)." : "."), "ok");
    $("claim").value = "";
    await loadState();
    setTimeout(refresh, 3000);
  });

  onSubmit("pw-form", async () => {
    if ($("pw-new").value !== $("pw-new2").value) throw new Error("The two passwords differ.");
    await api("/api/password", { password: $("pw-new").value, current: $("pw-current").value });
    ["pw-new", "pw-new2", "pw-current"].forEach((i) => { $(i).value = ""; });
    banner("Device password set.", "ok");
    await loadState();
  });

  onSubmit("settings-form", async () => {
    const res = await api("/api/settings", { enroll_url: $("enroll-url").value.trim() });
    $("enroll-url").value = res.enroll_url;
    banner("Enrollment server saved.", "ok");
  });

  onSubmit("wifi-form", async () => {
    const ssid = $("wifi-ssid").value;
    await api("/api/wifi", { ssid: ssid, password: $("wifi-pw").value });
    $("wifi-msg").textContent = "Joining " + ssid + "…";
    $("wifi-pw").value = "";
    pollWifi(0);
  });

  async function pollWifi(n) {
    try {
      const res = await api("/api/wifi");
      const job = res.job || {};
      if (job.state === "running" && n < 30) { setTimeout(() => pollWifi(n + 1), 2000); return; }
      $("wifi-msg").textContent = job.message || "";
    } catch (e) {
      $("wifi-msg").textContent = "Lost contact: if the node changed networks, reconnect to that network and open http://uapfinder.local.";
    }
  }

  $("wifi-scan").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    $("wifi-msg").textContent = "Scanning…";
    try {
      const res = await api("/api/wifi");
      const sel = $("wifi-ssid");
      sel.replaceChildren();
      (res.networks || []).forEach((n) => {
        const o = document.createElement("option");
        o.value = n.ssid;
        o.textContent = n.ssid + " (" + n.signal + "%" + (n.security ? ", " + n.security : ", open") + ")" + (n.in_use ? " – connected" : "");
        sel.appendChild(o);
      });
      $("wifi-form").hidden = !(res.networks || []).length;
      $("wifi-msg").textContent = res.error ? res.error : (res.networks || []).length ? "" : "No Wi-Fi networks found.";
    } catch (e) { $("wifi-msg").textContent = e.message; } finally { btn.disabled = false; }
  });

  document.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const action = btn.getAttribute("data-action");
      const ask = { "restart-node": "Restart the node software?", reboot: "Reboot the node?", poweroff: "Shut the node down? You will need to unplug and replug the power to start it again." };
      if (!window.confirm(ask[action])) return;
      try { await api("/api/action", { action: action }); banner("Requested: " + btn.textContent + ".", "ok"); }
      catch (e) { banner(e.message, "err"); }
    });
  });

  $("logout").addEventListener("click", async () => {
    try { await api("/api/logout", {}); } catch (e) { /* ignore */ }
    await loadState();
  });

  $("preview-toggle").addEventListener("click", () => setPreview(!previewOn));
  window.addEventListener("resize", drawOverlay);

  loadState().then(refresh).catch((e) => banner(e.message, "err"));
  setInterval(refresh, 5000);
})();
