// Forkful progressive enhancement. Every page works without this file. No animations, by request.
// CSP note: no inline handlers or styles. User text is only ever inserted with textContent.
(function () {
  "use strict";

  // Same rule as forkful/forms.py CALORIES_PATTERN: whole numbers 0..1,000,000, no sign, no leading zeros.
  var CALORIES_RE = /^(?:0|[1-9][0-9]{0,5}|1000000)$/;
  // Trim exactly the ASCII whitespace Python's strip(ASCII_SPACE) trims, so both sides agree.
  function asciiTrim(text) { return text.replace(/^[ \t\n\r\f\v]+|[ \t\n\r\f\v]+$/g, ""); }

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $all(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }
  function formatNumber(n) { return n.toLocaleString("en-US"); }

  // --- toasts: auto-dismiss after 4s, paused while hovered or focused ---
  function autoDismiss(toast) {
    var remaining = 4000, started = Date.now(), timer = null;
    function leave() { toast.remove(); }
    function start() { started = Date.now(); timer = window.setTimeout(leave, remaining); }
    function pause() { window.clearTimeout(timer); remaining -= Date.now() - started; }
    toast.addEventListener("mouseenter", pause);
    toast.addEventListener("mouseleave", start);
    toast.addEventListener("focusin", pause);
    toast.addEventListener("focusout", start);
    start();
  }

  function initToasts() { $all(".toast").forEach(autoDismiss); }

  // The same toast the server renders, built with textContent only.
  function showToast(message) {
    var container = $(".toasts");
    if (!container) return;
    var toast = document.createElement("div");
    toast.className = "toast toast-success";
    toast.setAttribute("role", "status");
    toast.tabIndex = 0;
    var icon = document.createElement("span");
    icon.className = "toast-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = "✅";
    var text = document.createElement("span");
    text.textContent = message;
    toast.appendChild(icon);
    toast.appendChild(text);
    container.appendChild(toast);
    autoDismiss(toast);
  }

  // --- fork dialog: the plain link still works; with JS it opens a <dialog> instead ---
  function initForkDialog() {
    $all("[data-fork-dialog]").forEach(function (trigger) {
      var dialog = document.getElementById(trigger.getAttribute("data-fork-dialog"));
      if (!dialog || typeof dialog.showModal !== "function") return;
      trigger.addEventListener("click", function (event) {
        // Let modified clicks (new tab, new window) follow the plain link to the fork page.
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        dialog.showModal();
        var input = $("[data-select-on-open]", dialog);
        if (input) { input.focus(); input.select(); }
      });
      $all("[data-close-dialog]", dialog).forEach(function (button) {
        button.addEventListener("click", function () { dialog.close(); });
      });
      dialog.addEventListener("close", function () { trigger.focus(); });
    });
  }

  // --- recipe form: rows, live total, photo drop zone and previews ---
  function initRecipeForm(form) {
    var rowsList = $("[data-ing-rows]", form);
    var template = $("[data-row-template]", form);
    var addButton = $("[data-add-row]", form);
    var liveTotal = $("[data-live-total]", form);
    var liveValue = $("[data-live-total-value]", form);

    function rows() { return $all("[data-ing-row]", rowsList); }

    function nextIndex() {
      var max = -1;
      $all("input[name^='ing-']", rowsList).forEach(function (input) {
        var match = /^ing-(\d+)-/.exec(input.name);
        if (match) max = Math.max(max, parseInt(match[1], 10));
      });
      return max + 1;
    }

    function renumber() {
      rows().forEach(function (row, i) {
        $all("[data-row-number]", row).forEach(function (span) { span.textContent = " " + (i + 1); });
      });
    }

    function updateTotal() {
      var total = 0;
      $all("[data-calories]", rowsList).forEach(function (input) {
        var value = asciiTrim(input.value);
        if (CALORIES_RE.test(value)) total += parseInt(value, 10);
      });
      liveValue.textContent = formatNumber(total);
    }

    function wireRow(row) {
      var remove = $("[data-remove-row]", row);
      remove.hidden = false;
      remove.addEventListener("click", function () {
        var all = rows();
        var index = all.indexOf(row);
        if (all.length === 1) {
          $all("input", row).forEach(function (input) { input.value = ""; });
          $("input", row).focus();
        } else {
          var next = all[index + 1] || all[index - 1];
          row.remove();
          $("input", next).focus();
        }
        renumber();
        updateTotal();
      });
    }

    function addRow() {
      var html = template.innerHTML
        .replace(/__I__/g, String(nextIndex()))
        .replace(/__N__/g, String(rows().length + 1));
      var holder = document.createElement("ol");
      holder.innerHTML = html;   // trusted markup from our own <template>, no user data
      var row = holder.firstElementChild;
      rowsList.appendChild(row);
      wireRow(row);
      renumber();
      $("input", row).focus();
    }

    if (rowsList && template && addButton) {
      rows().forEach(wireRow);
      addButton.hidden = false;
      addButton.addEventListener("click", addRow);
      liveTotal.hidden = false;
      rowsList.addEventListener("input", function (event) {
        if (event.target.hasAttribute("data-calories")) updateTotal();
      });
      updateTotal();
    }

    // Photos: drag-and-drop, blob previews, and a size pre-check before the upload starts.
    var dropzone = $("[data-dropzone]", form);
    var fileInput = dropzone && $("input[type=file]", dropzone);
    var previews = $("[data-previews]", form);
    var warning = $("[data-size-warning]", form);
    var maxRequest = parseInt(form.getAttribute("data-max-request"), 10);
    var maxPhoto = parseInt(form.getAttribute("data-max-photo"), 10);
    var previewUrls = [];

    function sizeProblem() {
      if (!fileInput) return null;
      var files = Array.prototype.slice.call(fileInput.files || []);
      var total = 0;
      for (var i = 0; i < files.length; i++) {
        total += files[i].size;
        if (files[i].size > maxPhoto) {
          return files[i].name + " is bigger than " + Math.round(maxPhoto / 1048576) + " MB. Please pick a smaller photo.";
        }
      }
      if (total > maxRequest - 1048576) {
        return "That's more than " + Math.round(maxRequest / 1048576) + " MB of photos for one save. Pick fewer now and add the rest in your next save.";
      }
      return null;
    }

    // A Cover radio on each new photo, value "new-<index>" (the server matches it to upload order).
    function coverChoice(index, fileName) {
      var label = document.createElement("label");
      label.className = "thumb-toggle thumb-cover";
      var radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "cover";
      radio.value = "new-" + index;
      var text = document.createElement("span");
      text.textContent = "⭐ Cover";
      var srText = document.createElement("span");
      srText.className = "sr-only";
      srText.textContent = " photo: " + fileName;
      text.appendChild(srText);
      label.appendChild(radio);
      label.appendChild(text);
      return label;
    }

    function showPreviews() {
      var previous = $("input[name=cover][value^='new-']:checked", previews);
      var keepIndex = previous ? previous.value : null;
      previewUrls.forEach(URL.revokeObjectURL);
      previewUrls = [];
      previews.textContent = "";
      var files = Array.prototype.slice.call(fileInput.files || []);
      files.forEach(function (file, index) {
        var li = document.createElement("li");
        li.className = "preview";
        if (/^image\/(jpeg|png|webp)$/.test(file.type)) {
          var url = URL.createObjectURL(file);
          previewUrls.push(url);
          var img = document.createElement("img");
          img.src = url;
          img.alt = "";
          li.appendChild(img);
        }
        var name = document.createElement("span");
        name.className = "preview-name";
        name.textContent = file.name;
        li.appendChild(name);
        li.appendChild(coverChoice(index, file.name));
        previews.appendChild(li);
      });
      // With no existing photos, the first new one is the cover by default, just like on the server.
      var existingCover = $("input[name=cover][value^='photo-']", form);
      var keep = keepIndex && $("input[value='" + keepIndex + "']", previews);
      if (keep) keep.checked = true;
      else if (!existingCover && files.length) $("input[name=cover]", previews).checked = true;
      previews.hidden = files.length === 0;
      var problem = sizeProblem();
      warning.textContent = problem || "";
      warning.hidden = !problem;
    }

    if (dropzone && fileInput) {
      fileInput.addEventListener("change", showPreviews);
      ["dragenter", "dragover"].forEach(function (type) {
        dropzone.addEventListener(type, function () { dropzone.classList.add("is-dragover"); });
      });
      ["dragleave", "drop"].forEach(function (type) {
        dropzone.addEventListener(type, function () { dropzone.classList.remove("is-dragover"); });
      });
    }

    form.addEventListener("submit", function (event) {
      var problem = sizeProblem();
      if (problem) {
        event.preventDefault();
        warning.textContent = problem;
        warning.hidden = false;
        warning.focus();   // role="alert" announces it; focus takes keyboard users to it
      }
    });

    // Delete a photo right away. Without JS the button submits to the same URL and the page reloads.
    $all("[data-delete-photo]", form).forEach(function (button) {
      button.addEventListener("click", function (event) {
        event.preventDefault();
        if (!window.confirm("Delete this photo? This can't be undone.")) return;
        var token = $("input[name=csrf_token]", form).value;
        button.disabled = true;
        fetch(button.formAction, {
          method: "POST",
          body: new URLSearchParams({ csrf_token: token }),
          headers: { "X-Requested-With": "fetch" },
          credentials: "same-origin"
        }).then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          var tile = button.closest("[data-photo-tile]");
          var tiles = $all("[data-photo-tile]", form);
          var next = tiles[tiles.indexOf(tile) + 1] || tiles[tiles.indexOf(tile) - 1];
          var wasCover = $("input[name=cover]", tile).checked;
          tile.remove();
          if (next) {
            if (wasCover) $("input[name=cover]", next).checked = true;   // the server promotes it too
            $("[data-delete-photo]", next).focus();
          } else {
            fileInput.focus();
          }
          showToast("Photo deleted.");
        }).catch(function () {
          button.disabled = false;
          warning.textContent = "Couldn't delete that photo. Please try again.";
          warning.hidden = false;
          warning.focus();
        });
      });
    });

    var summary = $("[data-error-summary]", form);
    if (summary) summary.focus();
  }

  // The tag search form: "Search (2)" as you pick, at most N picks, and on phones (one
  // swipeable row) the first ticked tag scrolled into view. Without JS it's a plain form.
  function initTagBar() {
    var form = $("[data-tag-bar]");
    if (!form) return;
    var boxes = $all("input[name=tag]", form);
    var label = $("[data-tag-search-label]", form);
    var max = parseInt(form.getAttribute("data-max-tags"), 10) || 10;

    function update() {
      var picked = boxes.filter(function (box) { return box.checked; }).length;
      label.textContent = picked ? "Search (" + picked + ")" : "Search";
      boxes.forEach(function (box) { if (!box.checked) box.disabled = picked >= max; });
    }
    boxes.forEach(function (box) { box.addEventListener("change", update); });
    update();

    var first = boxes.filter(function (box) { return box.checked; })[0];
    var chip = first && first.closest("label");
    var list = chip && chip.closest("ul");
    if (!list || list.scrollWidth <= list.clientWidth) return;
    var offset = chip.getBoundingClientRect().left - list.getBoundingClientRect().left;
    list.scrollLeft += offset - 16;
  }

  document.addEventListener("DOMContentLoaded", function () {
    initToasts();
    initTagBar();
    initForkDialog();
    $all("[data-recipe-form]").forEach(initRecipeForm);
  });
})();
