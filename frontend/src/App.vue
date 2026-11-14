<script setup>
import { computed, ref } from "vue";

const token = ref(localStorage.getItem("tide.token") || "");
const profile = ref(JSON.parse(localStorage.getItem("tide.profile") || "null"));
const username = ref("admin");
const password = ref("123456");
const loginError = ref("");
const batches = ref([]);
const current = ref(null);
const title = ref("");
const rawText = ref("");
const formError = ref("");
const busy = ref(false);

const settings = ref(null);
const queue = ref([]);
const settingsDraft = ref({ required_disposition: "keep", allow_edit: true });
const settingsError = ref("");
const settingsNotice = ref("");
const replayNotice = ref("");
const replayError = ref("");
const selectedRejectId = ref(null);
const replayLineDraft = ref("");
const replayBusy = ref(false);

const canWrite = computed(() => profile.value && profile.value.role === "writer");

const selectedReject = computed(() => {
  if (!current.value || !selectedRejectId.value) return null;
  return current.value.rejected.find((row) => row.id === selectedRejectId.value) || null;
});

const selectedHistory = computed(() => (selectedReject.value ? selectedReject.value.history : []));

const selectedAttempts = computed(() => {
  if (!current.value || !selectedRejectId.value) return [];
  return current.value.replays.filter((a) => a.reject_row_id === selectedRejectId.value);
});

const settingsDirty = computed(() => {
  if (!settings.value) return false;
  return (
    settingsDraft.value.required_disposition !== settings.value.required_disposition ||
    settingsDraft.value.allow_edit !== settings.value.allow_edit
  );
});

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token.value) headers.Authorization = `Bearer ${token.value}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data.detail || "请求没有完成";
    throw new Error(typeof message === "string" ? message : "请求没有完成");
  }
  return data;
}

async function refresh() {
  batches.value = await request("/api/batches");
  if (!current.value && batches.value.length) {
    await openBatch(batches.value[0].id);
  }
  await refreshReplaySide();
}

async function refreshReplaySide() {
  settings.value = await request("/api/replay-settings");
  settingsDraft.value = {
    required_disposition: settings.value.required_disposition,
    allow_edit: settings.value.allow_edit,
  };
  queue.value = await request("/api/replay-queue");
}

async function openBatch(id) {
  const previousId = current.value ? current.value.batch.id : null;
  current.value = await request(`/api/batches/${id}`);
  if (previousId !== id) {
    selectedRejectId.value = null;
    replayLineDraft.value = "";
    replayNotice.value = "";
    replayError.value = "";
  }
}

async function reloadCurrent() {
  if (!current.value) return;
  await openBatch(current.value.batch.id);
}

async function submitLogin() {
  loginError.value = "";
  try {
    const data = await request("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.value, password: password.value }),
    });
    token.value = data.access_token;
    profile.value = { username: data.username, role: data.role, title: data.title };
    localStorage.setItem("tide.token", token.value);
    localStorage.setItem("tide.profile", JSON.stringify(profile.value));
    await refresh();
  } catch (error) {
    loginError.value = error.message;
  }
}

async function submitBatch() {
  formError.value = "";
  busy.value = true;
  try {
    const data = await request("/api/batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title.value, raw_text: rawText.value }),
    });
    title.value = "";
    rawText.value = "";
    await refresh();
    current.value = data;
  } catch (error) {
    formError.value = error.message;
  } finally {
    busy.value = false;
  }
}

function logout() {
  token.value = "";
  profile.value = null;
  current.value = null;
  settings.value = null;
  queue.value = [];
  selectedRejectId.value = null;
  localStorage.removeItem("tide.token");
  localStorage.removeItem("tide.profile");
}

async function saveSettings() {
  settingsError.value = "";
  settingsNotice.value = "";
  try {
    const saved = await request("/api/replay-settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        required_disposition: settingsDraft.value.required_disposition,
        allow_edit: settingsDraft.value.allow_edit,
      }),
    });
    settings.value = saved;
    settingsNotice.value = "重放设置已保存";
    await reloadCurrent();
  } catch (error) {
    settingsError.value = error.message;
  }
}

function pickReject(row) {
  selectedRejectId.value = row.id;
  replayLineDraft.value = row.raw_line;
  replayNotice.value = "";
  replayError.value = "";
}

async function inspectQueued(row) {
  await openBatch(row.batch_id);
  const target = current.value.rejected.find((item) => item.id === row.id);
  if (target) pickReject(target);
}

async function enqueue(row) {
  replayError.value = "";
  try {
    await request(`/api/rejects/${row.id}/queue`, { method: "POST" });
    await reloadCurrent();
    await refreshReplaySide();
  } catch (error) {
    replayError.value = error.message;
  }
}

async function dequeue(row) {
  replayError.value = "";
  try {
    await request(`/api/rejects/${row.id}/queue`, { method: "DELETE" });
    await reloadCurrent();
    await refreshReplaySide();
  } catch (error) {
    replayError.value = error.message;
  }
}

async function dequeueFromQueue(row) {
  replayError.value = "";
  try {
    await request(`/api/rejects/${row.id}/queue`, { method: "DELETE" });
    await refreshReplaySide();
    if (current.value && current.value.batch.id === row.batch_id) await reloadCurrent();
  } catch (error) {
    replayError.value = error.message;
  }
}

async function chooseDisposition(row, disposition) {
  replayError.value = "";
  replayNotice.value = "";
  try {
    await request(`/api/rejects/${row.id}/disposition`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ disposition }),
    });
    await reloadCurrent();
    await refreshReplaySide();
    replayNotice.value = "去向已定下，当天不能更改";
  } catch (error) {
    replayError.value = error.message;
  }
}

async function runReplay(row) {
  replayError.value = "";
  replayNotice.value = "";
  replayBusy.value = true;
  try {
    const body = {};
    if (replayLineDraft.value.trim() !== row.raw_line) {
      body.raw_line = replayLineDraft.value.trim();
    }
    const result = await request(`/api/rejects/${row.id}/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (result.outcome === "success") {
      replayNotice.value = `重放成功，落到第 ${result.new_line_no} 行`;
    } else {
      replayNotice.value = `重放失败：${result.reason}`;
    }
    if (result.mismatch) {
      replayNotice.value += "（重放去向不符）";
    }
    await reloadCurrent();
    await refreshReplaySide();
    const still = current.value ? current.value.rejected.find((item) => item.id === row.id) : null;
    replayLineDraft.value = still ? still.raw_line : "";
  } catch (error) {
    replayError.value = error.message;
  } finally {
    replayBusy.value = false;
  }
}

function statusLabel(row) {
  if (row.status === "replayed") return "已重放";
  if (row.status === "withdrawn") return "已撤下";
  if (row.status === "queued") return "待重放";
  if (row.status === "replaying") return "重放中";
  return "未接纳";
}

function outcomeClass(outcome) {
  if (outcome === "重放成功") return "ok";
  if (outcome === "重放争用") return "clash";
  return "bad";
}

if (token.value) {
  refresh().catch(() => logout());
}
</script>

<template>
  <header class="top">
    <div>
      <p class="eyebrow">水文组 · 报文归集</p>
      <h1>潮位报文归集台</h1>
    </div>
    <div v-if="profile" class="who">
      <span>{{ profile.title }} {{ profile.username }}</span>
      <button class="ghost" @click="logout">离开</button>
    </div>
  </header>

  <main v-if="!profile" class="gate">
    <form @submit.prevent="submitLogin">
      <p>站码、时刻、潮位厘米、质量旗。一行一条，竖线分开。</p>
      <label>用户<input v-model="username" autocomplete="username" /></label>
      <label>口令<input v-model="password" type="password" autocomplete="current-password" /></label>
      <button>进入归集台</button>
      <p v-if="loginError" class="warn">{{ loginError }}</p>
    </form>
  </main>

  <main v-else class="desk">
    <section class="paste">
      <h2>贴入这一批</h2>
      <p class="rule">质量旗 A 实测，M 插补，B 可疑。同一站同一时刻只能落一次。</p>
      <label>批次名<input v-model="title" :disabled="!canWrite" placeholder="例如 堡镇 11-13 午班" /></label>
      <textarea
        v-model="rawText"
        :disabled="!canWrite"
        placeholder="BZ03|202611130900|176|A"
        spellcheck="false"
      />
      <button :disabled="!canWrite || busy" @click="submitBatch">归集这一批</button>
      <p v-if="!canWrite" class="quiet">档案员只能翻已落入的报文。</p>
      <p v-if="formError" class="warn">{{ formError }}</p>
    </section>

    <section class="slips">
      <h2>已收批次</h2>
      <button
        v-for="batch in batches"
        :key="batch.id"
        class="slip"
        :class="{ on: current && current.batch.id === batch.id }"
        @click="openBatch(batch.id)"
      >
        <strong>{{ batch.title }}</strong>
        <em>{{ batch.status }}</em>
        <span>落入 {{ batch.accepted_count }} 行 · 未接纳 {{ batch.rejected_count }} 行</span>
      </button>

      <article v-if="current" class="tape">
        <header>
          <h3>{{ current.batch.title }}</h3>
          <span>{{ current.batch.status }}</span>
        </header>
        <h4>已落库</h4>
        <table>
          <thead>
            <tr><th>行</th><th>站</th><th>时刻</th><th>厘米</th><th>旗</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in current.landed" :key="row.line_no">
              <td>
                {{ row.line_no }}
                <span v-if="row.replayed" class="tag">重放</span>
              </td>
              <td>{{ row.station_code }}</td>
              <td>{{ row.observed_at }}</td>
              <td>{{ row.tide_cm }}</td>
              <td>{{ row.flag_label }}</td>
            </tr>
            <tr v-if="!current.landed.length"><td colspan="5">这一批没有落入的行</td></tr>
          </tbody>
        </table>

        <h4>未接纳</h4>
        <ul class="rejects">
          <li
            v-for="row in current.rejected"
            :key="row.id"
            :class="{ picked: row.id === selectedRejectId, gone: row.status === 'replayed' }"
            @click="pickReject(row)"
          >
            <div class="reject-head">
              <code>{{ row.line_no }}. {{ row.raw_line }}</code>
              <span class="state" :class="row.status">{{ statusLabel(row) }}</span>
            </div>
            <span class="why">{{ row.reason }}</span>
            <span v-if="row.failure_count" class="fail">已失败 {{ row.failure_count }} 次</span>
            <span v-if="row.disposition_label" class="where">去向：{{ row.disposition_label }}</span>
            <span v-if="row.source_line_no" class="where">由第 {{ row.source_line_no }} 行换下</span>
          </li>
          <li v-if="!current.rejected.length">没有被挡下的行</li>
        </ul>

        <section v-if="selectedReject" class="compare">
          <header class="compare-head">
            <h4>未接纳详情 · 第 {{ selectedReject.line_no }} 行</h4>
            <button class="ghost dark" @click="selectedRejectId = null">收起</button>
          </header>
          <dl class="facts">
            <div><dt>当前去向</dt><dd>{{ selectedReject.disposition_label || "尚未选定" }}</dd></div>
            <div><dt>失败次数</dt><dd>{{ selectedReject.failure_count }}</dd></div>
            <div><dt>状态</dt><dd>{{ statusLabel(selectedReject) }}</dd></div>
            <div v-if="selectedReject.replaced_by_line_no">
              <dt>改由</dt><dd>第 {{ selectedReject.replaced_by_line_no }} 号未接纳行代替</dd>
            </div>
          </dl>

          <h5>两次原因对照</h5>
          <table class="reasons">
            <thead>
              <tr><th>次序</th><th>来源</th><th>原因</th><th>时间</th><th>操作人</th></tr>
            </thead>
            <tbody>
              <tr v-for="(entry, index) in selectedHistory" :key="index">
                <td>{{ index === 0 ? "首次" : `第 ${index} 次重放` }}</td>
                <td>{{ entry.source === "intake" ? "归集时" : "重放时" }}</td>
                <td>{{ entry.reason }}</td>
                <td>{{ entry.at || "—" }}</td>
                <td>{{ entry.by || "—" }}</td>
              </tr>
            </tbody>
          </table>

          <h5>重放记录</h5>
          <table v-if="selectedAttempts.length" class="reasons">
            <thead>
              <tr><th>结果</th><th>原行号</th><th>新行号</th><th>去向</th><th>说明</th><th>重放时原因</th><th>操作人</th></tr>
            </thead>
            <tbody>
              <tr v-for="attempt in selectedAttempts" :key="attempt.id">
                <td><span class="outcome" :class="outcomeClass(attempt.outcome)">{{ attempt.outcome }}</span></td>
                <td>{{ attempt.original_line_no }}</td>
                <td>{{ attempt.new_line_no || "—" }}</td>
                <td>{{ attempt.disposition_label || "—" }}</td>
                <td>
                  <span v-if="attempt.mismatch" class="mismatch">{{ attempt.mismatch_note }}</span>
                  <span v-else>—</span>
                </td>
                <td>{{ attempt.replay_reason || "—" }}</td>
                <td>{{ attempt.attempted_by }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="quiet">这一行还没有重放记录。</p>

          <template v-if="canWrite && (selectedReject.status === 'active' || selectedReject.status === 'queued')">
            <h5>重放操作</h5>
            <div class="row-actions">
              <button
                v-if="selectedReject.status === 'active'"
                class="line"
                @click="enqueue(selectedReject)"
              >加入待重放队列</button>
              <button
                v-if="selectedReject.status === 'queued'"
                class="line"
                @click="dequeue(selectedReject)"
              >撤出待重放队列</button>
            </div>
            <p class="quiet">选定去向，当天不能更改。设置要求去向必须为「{{
              settings ? settings.required_disposition_label : ""
            }}」。</p>
            <div class="row-actions">
              <button
                class="line"
                :class="{ on: selectedReject.disposition === 'keep' }"
                :disabled="!canWrite"
                @click="chooseDisposition(selectedReject, 'keep')"
              >原行留在未接纳</button>
              <button
                class="line"
                :class="{ on: selectedReject.disposition === 'replace' }"
                :disabled="!canWrite"
                @click="chooseDisposition(selectedReject, 'replace')"
              >原行撤下，换新未接纳行</button>
            </div>
            <label v-if="settings && settings.allow_edit" class="edit-line">
              重放时改行（留空按原行）
              <textarea v-model="replayLineDraft" spellcheck="false" />
            </label>
            <p v-else class="quiet">重放设置不许改行，重放按原行内容解析。</p>
            <div class="row-actions">
              <button
                :disabled="selectedReject.status !== 'queued' || replayBusy || !selectedReject.disposition"
                @click="runReplay(selectedReject)"
              >重放这一行</button>
            </div>
          </template>
          <p v-else-if="!canWrite" class="quiet">档案员只能查看重放情况。</p>
          <p v-else class="quiet">这一行已经离开未接纳，不能再重放。</p>
          <p v-if="replayNotice" class="notice">{{ replayNotice }}</p>
          <p v-if="replayError" class="warn">{{ replayError }}</p>
        </section>

        <h4>重放列表</h4>
        <table>
          <thead>
            <tr>
              <th>结果</th>
              <th>原行号</th>
              <th>新行号</th>
              <th>失败次数</th>
              <th>去向名</th>
              <th>重放时原因</th>
              <th>操作人</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="attempt in current.replays" :key="attempt.id">
              <td><span class="outcome" :class="outcomeClass(attempt.outcome)">{{ attempt.outcome }}</span></td>
              <td>{{ attempt.original_line_no }}</td>
              <td>{{ attempt.new_line_no || "—" }}</td>
              <td>{{ attempt.failure_count }}</td>
              <td>{{ attempt.disposition_label || "—" }}</td>
              <td>
                {{ attempt.replay_reason || "—" }}
                <span v-if="attempt.mismatch" class="mismatch">{{ attempt.mismatch_note }}</span>
              </td>
              <td>{{ attempt.attempted_by }}</td>
              <td>{{ attempt.attempted_at }}</td>
            </tr>
            <tr v-if="!current.replays.length"><td colspan="8">这一批还没有重放过</td></tr>
          </tbody>
        </table>
      </article>
    </section>

    <aside class="replay-side">
      <section class="panel">
        <h2>重放设置</h2>
        <p class="rule">设置要求去向与选定去向不一致时，重放记录会写明「重放去向不符」。重放进行时设置不能改。</p>
        <template v-if="settings">
          <label>
            要求去向
            <select v-model="settingsDraft.required_disposition" :disabled="!canWrite">
              <option v-for="item in settings.dispositions" :key="item.value" :value="item.value">
                {{ item.label }}
              </option>
            </select>
          </label>
          <label class="check">
            <input v-model="settingsDraft.allow_edit" type="checkbox" :disabled="!canWrite" />
            重放时允许改行
          </label>
          <p v-if="settings.replay_in_progress" class="frozen">重放进行中，暂不能改设置。</p>
          <p class="quiet">
            上次由 {{ settings.updated_by || "—" }} 于 {{ settings.updated_at || "—" }} 保存。
          </p>
          <button :disabled="!canWrite || !settingsDirty || settings.replay_in_progress" @click="saveSettings">
            保存重放设置
          </button>
          <p v-if="settingsNotice" class="notice">{{ settingsNotice }}</p>
          <p v-if="settingsError" class="warn">{{ settingsError }}</p>
        </template>
        <p v-else class="quiet">正在读取设置。</p>
      </section>

      <section class="panel">
        <header class="panel-head">
          <h2>待重放队列</h2>
          <span class="count">{{ queue.length }} 行</span>
        </header>
        <ul class="queue">
          <li v-for="row in queue" :key="row.id">
            <div class="queue-head">
              <code>第 {{ row.line_no }} 行 · {{ row.batch_title }}</code>
              <span class="state" :class="row.disposition ? 'disposition' : 'active'">
                {{ row.disposition_label || "未选去向" }}
              </span>
            </div>
            <code class="raw">{{ row.raw_line }}</code>
            <span class="why">{{ row.reason }}</span>
            <span v-if="row.failure_count" class="fail">已失败 {{ row.failure_count }} 次</span>
            <div v-if="canWrite" class="row-actions">
              <button class="line" @click="inspectQueued(row)">查看详情</button>
              <button class="line" @click="dequeueFromQueue(row)">撤出队列</button>
            </div>
          </li>
          <li v-if="!queue.length" class="quiet">队列是空的。到未接纳行里挑一行加入。</li>
        </ul>
      </section>
    </aside>
  </main>
</template>

<style scoped>
.top, .desk, .gate { width: min(1360px, calc(100% - 32px)); margin: 0 auto; }
.top { display: flex; justify-content: space-between; align-items: end; padding: 28px 0 12px; }
.eyebrow { letter-spacing: 0.18em; text-transform: uppercase; color: #d7a15f; font-size: 12px; margin: 0 0 6px; }
h1 { margin: 0; font-weight: 560; font-size: 34px; }
h2, h3, h4, h5 { margin: 0; }
h5 { margin: 14px 0 8px; font-size: 14px; color: #3a4757; }
.who { display: flex; gap: 12px; align-items: center; }
.ghost { background: transparent; color: #f3efe2; border: 1px solid rgba(243, 239, 226, 0.35); padding: 6px 12px; }
.ghost.dark { color: #1c2430; border-color: #c9bfa6; padding: 4px 10px; font-size: 13px; }
.gate { padding: 24px 0 48px; }
.gate form, .paste, .tape, .panel {
  background: #f3efe2;
  color: #1c2430;
  padding: 22px;
}
.gate form { width: min(420px, 100%); display: grid; gap: 12px; }
label { display: grid; gap: 6px; font-size: 14px; }
label.check { display: flex; gap: 8px; align-items: center; }
input, textarea, select {
  width: 100%;
  border: 0;
  background: #fffdf8;
  color: #1c2430;
  padding: 10px 12px;
  box-shadow: inset 0 0 0 1px #d9d0bc;
}
select { cursor: pointer; }
textarea { min-height: 220px; font-family: "IBM Plex Mono", Consolas, monospace; line-height: 1.6; }
.edit-line textarea { min-height: 72px; }
button { background: #9d2c2c; color: #fffaf2; padding: 10px 14px; }
button.line { background: transparent; color: #1c2430; border: 1px solid #c9bfa6; padding: 6px 10px; font-size: 13px; }
button.line.on { border-color: #9d2c2c; background: #9d2c2c; color: #fffaf2; }
.desk { display: grid; grid-template-columns: 320px minmax(0, 1fr) 340px; gap: 18px; padding-bottom: 48px; align-items: start; }
.paste, .slips, .replay-side { display: grid; gap: 12px; align-content: start; }
.panel { display: grid; gap: 10px; }
.panel-head { display: flex; justify-content: space-between; align-items: baseline; }
.rule, .quiet { margin: 0; color: #5c5648; font-size: 13px; }
.frozen { margin: 0; color: #9d2c2c; font-size: 13px; }
.slip {
  text-align: left;
  background: transparent;
  color: #f3efe2;
  border: 1px solid rgba(243, 239, 226, 0.2);
  padding: 12px 14px;
  display: grid;
  gap: 4px;
}
.slip.on { border-color: #d7a15f; }
.slip em { font-style: normal; color: #d7a15f; }
.slip span { color: #a8a293; }
.tape header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.tape h4 { margin: 18px 0 8px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid #e4dcc8; font-size: 13px; vertical-align: top; }
ul { list-style: none; padding: 0; margin: 0; }
.rejects { display: grid; gap: 8px; }
.rejects > li { display: grid; gap: 3px; padding: 8px 10px; border: 1px solid #e4dcc8; background: #fffdf8; cursor: pointer; }
.rejects > li.picked { border-color: #9d2c2c; box-shadow: inset 3px 0 0 #9d2c2c; }
.rejects > li.gone { opacity: 0.62; }
.reject-head { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
code { font-family: Consolas, monospace; }
code.raw { display: block; color: #3a4757; }
.why { color: #5c5648; font-size: 13px; }
.fail { color: #9d2c2c; font-size: 12px; }
.where { color: #5c6d55; font-size: 12px; }
.state { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #e4dcc8; color: #3a4757; white-space: nowrap; }
.state.replayed { background: #dce8d5; color: #3d5a34; }
.state.withdrawn { background: #efe0d5; color: #8a4b23; }
.state.queued { background: #f0e4c4; color: #7a5a12; }
.state.replaying { background: #dfe6f2; color: #2f466e; }
.state.disposition { background: #dce8d5; color: #3d5a34; }
.tag { font-size: 11px; color: #fffaf2; background: #5c6d55; padding: 1px 6px; border-radius: 3px; margin-left: 4px; }
.compare { margin-top: 16px; padding: 14px; background: #fffdf8; border: 1px solid #d9d0bc; display: grid; gap: 6px; }
.compare-head { display: flex; justify-content: space-between; align-items: baseline; }
.facts { display: flex; flex-wrap: wrap; gap: 16px; margin: 4px 0 0; }
.facts div { display: grid; gap: 2px; }
.facts dt { font-size: 12px; color: #7b7362; }
.facts dd { margin: 0; font-size: 14px; }
.reasons { margin-bottom: 4px; }
.outcome { font-size: 12px; padding: 2px 8px; border-radius: 999px; white-space: nowrap; }
.outcome.ok { background: #dce8d5; color: #3d5a34; }
.outcome.bad { background: #f2dcdc; color: #8c2b2b; }
.outcome.clash { background: #f0e4c4; color: #7a5a12; }
.mismatch { display: inline-block; margin-left: 6px; color: #8c2b2b; font-size: 12px; font-weight: 600; }
.row-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.notice { margin: 0; color: #3d5a34; font-size: 13px; }
.warn { color: #9d2c2c; margin: 0; font-size: 13px; }
.queue { display: grid; gap: 10px; }
.queue > li { display: grid; gap: 4px; padding: 10px; background: #fffdf8; border: 1px solid #e4dcc8; }
.queue-head { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
.count { font-size: 12px; color: #7b7362; }
@media (max-width: 1180px) {
  .desk { grid-template-columns: 1fr; }
}
</style>
