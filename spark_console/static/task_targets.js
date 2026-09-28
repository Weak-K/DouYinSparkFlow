(() => {
  const form = document.querySelector("#task-create-form");
  const account = document.querySelector("#task-account");
  const filter = document.querySelector("#task-target-filter");
  const refresh = document.querySelector("#refresh-targets");
  const list = document.querySelector("#task-target-list");
  const chosen = document.querySelector("#task-target-chosen");
  const targets = document.querySelector("#task-targets");
  const status = document.querySelector("#target-status");
  if (!form || !account || !filter || !refresh || !list || !chosen || !targets || !status) return;

  let catalog = [];
  const selected = new Map();

  const keyOf = (name, secUid) => secUid || `name:${name}`;

  function formatScannedAt(value) {
    if (!value) return "";
    const moment = new Date(value);
    if (Number.isNaN(moment.getTime())) return "";
    const pad = (number) => String(number).padStart(2, "0");
    return `（更新于 ${pad(moment.getMonth() + 1)}-${pad(moment.getDate())} ${pad(
      moment.getHours()
    )}:${pad(moment.getMinutes())}）`;
  }

  function syncHidden() {
    targets.value = JSON.stringify(Array.from(selected.values()));
  }

  function renderChosen() {
    chosen.replaceChildren();
    for (const [key, item] of selected) {
      const chip = document.createElement("span");
      chip.className = "target-chip";
      const label = document.createElement("span");
      label.textContent = item.name;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "target-chip-remove";
      remove.setAttribute("aria-label", `移除 ${item.name}`);
      remove.textContent = "×";
      remove.addEventListener("click", () => {
        selected.delete(key);
        renderChosen();
        renderList();
        syncHidden();
      });
      chip.append(label, remove);
      chosen.appendChild(chip);
    }
    chosen.hidden = selected.size === 0;
  }

  function toggle(name, secUid, on) {
    const key = keyOf(name, secUid);
    if (on) selected.set(key, { name, sec_uid: secUid });
    else selected.delete(key);
    renderChosen();
    syncHidden();
  }

  function makeRow(name, secUid, { custom = false } = {}) {
    const row = document.createElement("label");
    row.className = "target-item";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = selected.has(keyOf(name, secUid));
    box.addEventListener("change", () => {
      toggle(name, secUid, box.checked);
      if (custom) {
        filter.value = "";
        renderList();
        return;
      }
      renderList();
    });
    const text = document.createElement("span");
    text.className = "target-item-name";
    text.textContent = custom ? `添加「${name}」` : name;
    row.append(box, text);
    return row;
  }

  function renderList() {
    const keyword = filter.value.trim();
    const lowered = keyword.toLowerCase();
    const matched = catalog.filter(
      (item) => !lowered || item.name.toLowerCase().includes(lowered)
    );
    const exact = catalog.some((item) => item.name === keyword);
    list.replaceChildren();
    if (keyword && !exact) list.appendChild(makeRow(keyword, "", { custom: true }));
    for (const item of matched) {
      list.appendChild(makeRow(item.name, item.sec_uid || ""));
    }
    list.hidden = list.childElementCount === 0;
  }

  async function loadTargets() {
    refresh.disabled = true;
    status.textContent = "正在读取该账号已保存的好友名单…";
    catalog = [];
    selected.clear();
    renderChosen();
    syncHidden();
    list.replaceChildren();
    try {
      const prefix = account.dataset.conversationPrefix || "/accounts";
      const response = await fetch(
        `${prefix}/${encodeURIComponent(account.value)}/conversations`,
        { credentials: "same-origin", headers: { Accept: "application/json" } }
      );
      const body = await response.json();
      if (!response.ok) throw new Error(body.message || "好友名单读取失败");
      catalog = (body.items || []).map((item) => ({
        name: item.name,
        sec_uid: item.sec_uid || "",
      }));
      const scanned = formatScannedAt(body.scanned_at);
      status.textContent = catalog.length
        ? `已保存 ${catalog.length} 位好友${scanned}，可一次勾选多位；名单外的昵称直接输入后按回车添加`
        : "尚未保存好友名单（下次执行任务时会自动读取），可直接输入昵称后按回车添加";
    } catch (error) {
      status.textContent = error.message || "好友名单读取失败，可直接输入昵称后按回车添加";
    } finally {
      renderList();
      refresh.disabled = false;
    }
  }

  account.addEventListener("change", loadTargets);
  refresh.addEventListener("click", loadTargets);
  filter.addEventListener("input", renderList);
  filter.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    const keyword = filter.value.trim();
    if (!keyword) return;
    const exact = catalog.find((item) => item.name === keyword);
    toggle(exact?.name || keyword, exact?.sec_uid || "", true);
    filter.value = "";
    renderList();
    filter.focus();
  });
  form.addEventListener("submit", (event) => {
    if (selected.size === 0) {
      event.preventDefault();
      status.textContent = "请至少选择一位好友；名单里没有的话，输入昵称后按回车即可添加";
      filter.focus();
      return;
    }
    syncHidden();
  });

  loadTargets();
})();
