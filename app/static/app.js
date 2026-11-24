(function () {
  "use strict";

  function bindTabs(root) {
    root.querySelectorAll("[data-tabs]").forEach(function (box) {
      box.querySelectorAll("[data-tab]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var key = btn.getAttribute("data-tab");
          box.setAttribute("data-current", key);
          box.querySelectorAll("[data-tab]").forEach(function (b) {
            b.classList.toggle("on", b === btn);
          });
          box.querySelectorAll("[data-pane]").forEach(function (pane) {
            pane.classList.toggle("hide", pane.getAttribute("data-pane") !== key);
          });
        });
      });
    });
  }

  function restoreTabs(root, chosen) {
    root.querySelectorAll("[data-tabs]").forEach(function (box, n) {
      var key = chosen[n];
      var btn = key && box.querySelector('[data-tab="' + key + '"]');
      if (btn) { btn.click(); }
    });
  }

  var narrow = window.matchMedia("(max-width: 760px)");

  // 좁은 화면의 가로 메뉴에서 현재 메뉴가 보이도록 옮긴다.
  function showActiveMenu() {
    var nav = document.querySelector(".side nav");
    var on = nav && nav.querySelector("a.on");
    if (!on || !narrow.matches) { return; }
    nav.scrollLeft = Math.max(0, on.offsetLeft - (nav.clientWidth - on.offsetWidth) / 2);
  }

  // 좁은 화면에서는 프로젝트별 기한 표를 접어 두고, 주소의 #p번호 가 가리키는 것만 편다.
  function openTarget() {
    var id = window.location.hash.slice(1);
    var box = id && document.getElementById(id);
    var fold = box && box.querySelector("details.fold");
    if (fold) { fold.open = true; box.scrollIntoView(); }
  }

  function foldProjects() {
    if (!narrow.matches) { return; }
    document.querySelectorAll("details.fold").forEach(function (d) { d.open = false; });
    openTarget();
  }

  showActiveMenu();
  foldProjects();
  window.addEventListener("hashchange", openTarget);

  var main = document.getElementById("content");
  if (!main) { return; }
  bindTabs(main);

  var seconds = parseInt(main.getAttribute("data-refresh") || "0", 10);
  if (!seconds) { return; }

  setInterval(function () {
    if (document.hidden) { return; }
    fetch(window.location.href, { credentials: "same-origin", cache: "no-store" })
      .then(function (res) {
        if (res.redirected || !res.ok) { window.location.reload(); return null; }
        return res.text();
      })
      .then(function (html) {
        if (!html) { return; }
        var fresh = new DOMParser().parseFromString(html, "text/html").getElementById("content");
        if (!fresh) { return; }
        var chosen = Array.prototype.map.call(main.querySelectorAll("[data-tabs]"), function (box) {
          return box.getAttribute("data-current");
        });
        main.replaceChildren.apply(main, Array.prototype.slice.call(fresh.childNodes));
        bindTabs(main);
        restoreTabs(main, chosen);
      })
      .catch(function () { /* 다음 주기에 다시 시도 */ });
  }, seconds * 1000);
})();
