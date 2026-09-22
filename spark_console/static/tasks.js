(() => {
  const account = document.querySelector("#task-account");
  const target = document.querySelector("#task-target");
  const targetSecUid = document.querySelector("#task-target-sec-uid");
  const options = document.querySelector("#task-target-options");
  const refresh = document.querySelector("#refresh-targets");
  const status = document.querySelector("#target-status");
  if (!account || !target || !targetSecUid || !options || !refresh || !status) return;

  function bindSelectedIdentity() {
    const selected = Array.from(options.options).find((item) => item.value === target.value);
    targetSecUid.value = selected?.dataset.secUid || "";
  }

  function formatScannedAt(value) {
    if (!value) return "";
    const moment = new Date(value);
    if (Number.isNaN(moment.getTime())) return "";
    const pad = (number) => String(number).padStart(2, "0");
    return `（更新于 ${pad(moment.getMonth() + 1)}-${pad(moment.getDate())} ${pad(
      moment.getHours()
    )}:${pad(moment.getMinutes())}）`;
  }

  async function loadTargets() {
    refresh.disabled = true;
    status.textContent = "正在读取该账号已保存的好友名单…";
    options.replaceChildren();
    try {
      const prefix = account.dataset.conversationPrefix || "/accounts";
      const response = await fetch(`${prefix}/${encodeURIComponent(account.value)}/conversations`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.message || "好友名单读取失败");
      for (const item of body.items || []) {
        const option = document.createElement("option");
        option.value = item.name;
        option.dataset.secUid = item.sec_uid || "";
        options.appendChild(option);
      }
      const scanned = formatScannedAt(body.scanned_at);
      status.textContent = body.items?.length
        ? `已保存 ${body.items.length} 个好友可选${scanned}；每次执行任务时会自动补全名单，更多好友会陆续出现`
        : "尚未保存好友名单（下次执行任务时会自动读取），也可直接手动输入准确昵称";
    } catch (error) {
      status.textContent = error.message || "好友名单读取失败，仍可手动输入";
    } finally {
      refresh.disabled = false;
    }
  }

  account.addEventListener("change", () => {
    target.value = "";
    targetSecUid.value = "";
    loadTargets();
  });
  target.addEventListener("input", bindSelectedIdentity);
  refresh.addEventListener("click", loadTargets);
  loadTargets();
})();
