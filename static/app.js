// CareFlow AI - small progressive enhancements. The app works without JavaScript except the chat.
(function () {
  "use strict";

  // Slow AI buttons: show a spinner and stop double submits.
  document.querySelectorAll("form[data-busy]").forEach(function (f) {
    f.addEventListener("submit", function () {
      f.classList.add("busy");
      setTimeout(function () { f.querySelectorAll("button").forEach(function (b) { b.disabled = true; }); }, 0);
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (ev) { if (!window.confirm(f.dataset.confirm)) ev.preventDefault(); });
  });

  document.querySelectorAll(".print-btn").forEach(function (b) { b.addEventListener("click", function () { window.print(); }); });

  // Larger text (asked for in the clinician trial): 100% -> 115% -> 130%, remembered on this computer.
  var sizes = ["", "fs-1", "fs-2"];
  function applySize(i) {
    document.documentElement.classList.remove("fs-1", "fs-2");
    if (sizes[i]) document.documentElement.classList.add(sizes[i]);
  }
  var cur = Number(localStorage.getItem("cf_font") || 0) % sizes.length;
  applySize(cur);
  var fbtn = document.getElementById("font-size");
  if (fbtn) fbtn.addEventListener("click", function () {
    cur = (cur + 1) % sizes.length; localStorage.setItem("cf_font", String(cur)); applySize(cur);
  });

  // ------------------------------------------------------------------ chat
  var box = document.getElementById("chat");
  if (!box) return;
  var log = document.getElementById("chat-log");
  var form = document.getElementById("chat-form");
  var input = document.getElementById("chat-q");
  var send = document.getElementById("chat-send");
  var csrf = (document.querySelector('meta[name="csrf"]') || {}).content;
  var busy = false;

  function scroll() { log.scrollTop = log.scrollHeight; }
  scroll();

  function add(html) {
    var empty = log.querySelector(".empty");
    if (empty) empty.remove();
    var tmp = document.createElement("div");
    tmp.innerHTML = html;
    while (tmp.firstChild) log.appendChild(tmp.firstChild);
    scroll();
  }

  function thinking() {
    var el = document.createElement("div");
    el.className = "bubble bot thinking";
    var steps = ["Reading the question", "Looking up the ward record", "Checking citations"];
    var i = 0;
    el.innerHTML = '<span class="dots"><i></i><i></i><i></i></span> <span class="tstep">' + steps[0] + "…</span>";
    log.appendChild(el);
    scroll();
    var t = setInterval(function () { i = Math.min(i + 1, steps.length - 1); el.querySelector(".tstep").textContent = steps[i] + "…"; }, 2200);
    return function () { clearInterval(t); el.remove(); };
  }

  function ask(q) {
    q = (q || "").trim();
    if (!q || busy) return;
    busy = true; send.disabled = true; input.value = "";
    var tmp = document.createElement("div"); tmp.textContent = q;
    add('<div class="bubble me">' + tmp.innerHTML + "</div>");
    var stop = thinking();
    fetch("/api/ask", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({ q: q, focus: box.dataset.focus || null })
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        stop();
        if (!res.ok) { add('<div class="bubble bot err">' + (res.j.error || "Something went wrong") + "</div>"); return; }
        add(res.j.bot_html);
      })
      .catch(function () { stop(); add('<div class="bubble bot err">Network error – is the app still running?</div>'); })
      .finally(function () { busy = false; send.disabled = false; input.focus(); });
  }

  form.addEventListener("submit", function (ev) { ev.preventDefault(); ask(input.value); });
  input.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); ask(input.value); }
  });
  box.querySelectorAll(".hint").forEach(function (b) { b.addEventListener("click", function () { ask(b.dataset.q); }); });
})();
