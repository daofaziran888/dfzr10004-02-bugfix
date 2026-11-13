<template>
  <section class="page" data-module="taskplan">
    <header class="page-head">
      <div>
        <h2>采集任务编排管理</h2>
        <p class="page-desc">维护采集计划的站点配置、频率设置与资源分配；计划按「草稿 → 待执行 → 执行中 → 已暂停 → 已归档」相邻流转，暂停只作用于执行中，归档只作用于已暂停。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记采集计划</button>
        <button class="btn" type="button" @click="exportRows">导出采集任务编排清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>计划编号</span>
        <input v-model="keyword" placeholder="按计划编号检索" />
      </label>
      <label class="filter-item">
        <span>计划阶段</span>
        <select v-model="statusFilter">
          <option value="">全部阶段</option>
          <option v-for="stage in stages" :key="stage" :value="stage">{{ stage }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>版本</th>
          <th>可执行动作（按当前阶段给出）</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">
            <template v-if="column === '计划状态'">
              <span :class="stageClass(String(row.status))">{{ row.status }}</span>
            </template>
            <template v-else>
              <button v-if="column === '计划编号'" class="link" type="button" @click="openDetail(row.id)">
                {{ row[column] ?? '—' }}
              </button>
              <template v-else>{{ row[column] ?? '—' }}</template>
            </template>
          </td>
          <td>v{{ row.version }}</td>
          <td class="row-actions">
            <template v-if="row.allowed_actions?.length">
              <button
                v-for="action in row.allowed_actions"
                :key="action"
                class="link"
                :class="{ danger: action === '归档计划' }"
                :disabled="actingId === row.id"
                type="button"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
            </template>
            <span v-else class="muted">无可用动作</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无采集任务编排数据，可先登记采集计划</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集任务编排记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 计划联动的采集结果校验清单 -->
    <section class="linked-panel">
      <header class="linked-head">
        <h3>采集结果校验清单（随计划阶段联动）</h3>
        <label class="filter-item inline">
          <input v-model="pendingOnly" type="checkbox" @change="loadTasks" />
          仅看待处理项
        </label>
      </header>
      <table class="data-table">
        <thead>
          <tr><th>计划编号</th><th>清单状态</th><th>校验结论</th><th>结论时间</th><th>版本</th><th>结果回写</th></tr>
        </thead>
        <tbody>
          <tr v-for="task in tasks" :key="String(task.id)">
            <td>{{ task['计划编号'] }}</td>
            <td><span :class="stageClass(String(task.status))">{{ task.status }}</span></td>
            <td>
              <span v-if="task.result_status" :class="stageClass(String(task.result_status))">{{ task.result_status }}</span>
              <span v-else class="muted">待回写</span>
            </td>
            <td>{{ formatTs(task.result_at) }}</td>
            <td>v{{ task.version }}</td>
            <td class="row-actions">
              <button class="link" type="button" :disabled="task.status === '已归档'" @click="writeResult(task, true)">
                回写通过
              </button>
              <button class="link danger" type="button" :disabled="task.status === '已归档'" @click="writeResult(task, false)">
                回写不通过
              </button>
            </td>
          </tr>
          <tr v-if="!tasks.length">
            <td colspan="6" class="empty-state">暂无校验清单项（计划启动后自动生成待处理项）</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 下发任务统一判定：仅执行中计划可继续下发，已暂停/已归档不在其中 -->
    <section class="linked-panel">
      <header class="linked-head">
        <h3>可下发任务的计划（校验入口共用口径）</h3>
        <button class="btn ghost" type="button" @click="loadDispatchable">刷新</button>
      </header>
      <p class="panel-hint">
        已暂停、已归档的计划不会出现在这里，关联模块不会再为其消耗配额；
        当前可下发 {{ dispatchable.length }} 条：
        <span v-for="plan in dispatchable" :key="plan.id" class="action-chip">{{ plan['计划编号'] }}（{{ plan.status }}）</span>
      </p>
    </section>

    <PlanDrawer :view="lifecycle" @close="lifecycle = null" />
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, postAction, ApiConflictError } from '@/api/client'
import PlanDrawer from './PlanDrawer.vue'
import { formatTs, stageClass, type LifecycleView, type PlanRow } from './lifecycle'

type TaskRow = Record<string, string | number | boolean | null> & {
  id: number
  status: string
  version: number
  result_status: string | null
  result_at?: number
}

const ENDPOINT = '/api/taskplan'
const columns = ['计划编号', '目标站点', '采集频率', '数据范围', '优先级', '资源配额', '预计用时', '计划状态']
const stages = ['草稿', '待执行', '执行中', '已暂停', '已归档']

const rows = ref<PlanRow[]>([])
const tasks = ref<TaskRow[]>([])
const dispatchable = ref<PlanRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const pendingOnly = ref(false)
const actingId = ref<number | null>(null)
const lifecycle = ref<LifecycleView | null>(null)

const stats = computed(() => [
  { label: '采集计划总数', value: rows.value.length },
  { label: '执行中计划', value: rows.value.filter((r) => r.status === '执行中').length },
  { label: '已暂停计划', value: rows.value.filter((r) => r.status === '已暂停').length },
  { label: '已归档计划', value: rows.value.filter((r) => r.status === '已归档').length },
])

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '采集计划登记入口尚未接入审批流'
}

async function runAction(action: string, row: PlanRow) {
  errorMessage.value = ''
  actingId.value = row.id
  try {
    // 携带当前版本做乐观并发控制：两个入口同时提交时只有一个能推进。
    const result = await postAction(`${ENDPOINT}/${row.id}/actions`, {
      values: {
        action,
        expected_version: row.version,
        // 撤销暂停仅运营管理员可执行，且只能回到执行中。
        ...(action === '恢复执行' ? { operator_role: '运营管理员' } : {}),
      },
    })
    if (!result.ok) {
      errorMessage.value = result.message
    }
  } catch (error) {
    if (error instanceof ApiConflictError) {
      errorMessage.value = `并发冲突：${error.message}`
    } else {
      errorMessage.value = error instanceof Error ? error.message : '采集任务编排操作失败'
    }
  } finally {
    actingId.value = null
    await Promise.all([reload(), loadTasks(), loadDispatchable()])
  }
}

async function writeResult(task: TaskRow, passed: boolean) {
  errorMessage.value = ''
  try {
    const result = await postAction(`/api/verify/tasks/${task.id}/result`, {
      values: { passed, summary: passed ? '页面回写：校验通过' : '页面回写：校验不通过，待人工复核' },
    })
    if (!result.ok) errorMessage.value = result.message
  } catch (error) {
    if (error instanceof ApiConflictError) {
      errorMessage.value = `并发冲突：${error.message}`
    } else {
      errorMessage.value = error instanceof Error ? error.message : '校验结果回写失败'
    }
  }
  await loadTasks()
}

async function openDetail(id: number) {
  lifecycle.value = await fetchJson<LifecycleView>(`${ENDPOINT}/${id}/lifecycle`)
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const payload = await fetchJson<{ items: PlanRow[]; total: number }>(`${ENDPOINT}?${query.toString()}`)
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务编排列表读取失败'
  }
}

async function loadTasks() {
  const query = new URLSearchParams()
  if (pendingOnly.value) query.set('pending_only', 'true')
  try {
    const payload = await fetchJson<{ items: TaskRow[] }>(`/api/verify/tasks?${query.toString()}`)
    tasks.value = payload.items ?? []
  } catch {
    // 清单加载失败不阻塞计划主表。
  }
}

async function loadDispatchable() {
  try {
    const payload = await fetchJson<{ items: PlanRow[] }>(`${ENDPOINT}/dispatchable`)
    dispatchable.value = payload.items ?? []
  } catch {
    dispatchable.value = []
  }
}

onMounted(() => {
  void reload()
  void loadTasks()
  void loadDispatchable()
})
</script>
