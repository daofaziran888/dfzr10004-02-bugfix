<template>
  <section class="page" data-module="verify">
    <header class="page-head">
      <div>
        <h2>采集结果校验管理</h2>
        <p class="page-desc">维护采集结果的校验规则定义、阈值配置与阻断策略；规则按「草稿 → 运行中 → 已暂停 → 已归档」相邻流转，计划侧的校验清单与结果回写见采集任务编排页联动区。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记校验规则</button>
        <button class="btn" type="button" @click="exportRows">导出采集结果校验清单</button>
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
        <span>规则编号</span>
        <input v-model="keyword" placeholder="按规则编号检索" />
      </label>
      <label class="filter-item">
        <span>规则阶段</span>
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
          <th>规则阶段</th>
          <th>版本</th>
          <th>可执行动作（按当前阶段给出）</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td><span :class="stageClass(String(row.status))">{{ row.status }}</span></td>
          <td>v{{ row.version }}</td>
          <td class="row-actions">
            <template v-if="row.allowed_actions?.length">
              <button
                v-for="action in row.allowed_actions"
                :key="action"
                class="link"
                :class="{ danger: action === '归档规则' }"
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
          <td :colspan="columns.length + 3" class="empty-state">暂无采集结果校验数据，可先登记校验规则</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集结果校验记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, postAction, ApiConflictError } from '@/api/client'
import { stageClass } from '@/views/taskplan/lifecycle'

type Row = Record<string, string | number | boolean | null | string[]> & {
  id: number
  status: string
  version: number
  allowed_actions: string[]
}

const ENDPOINT = '/api/verify'
const columns = ['规则编号', '校验对象', '校验类型', '校验表达式', '阈值配置', '采样比例', '阻断级别', '最近校验']
const stages = ['草稿', '运行中', '已暂停', '已归档']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const actingId = ref<number | null>(null)

const stats = computed(() => [
  { label: '校验规则总数', value: rows.value.length },
  { label: '运行中规则', value: rows.value.filter((r) => r.status === '运行中').length },
  { label: '已暂停规则', value: rows.value.filter((r) => r.status === '已暂停').length },
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
  errorMessage.value = '校验规则登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  actingId.value = row.id
  try {
    const result = await postAction(`${ENDPOINT}/${row.id}/actions`, {
      values: { action, expected_version: row.version },
    })
    if (!result.ok) errorMessage.value = result.message
  } catch (error) {
    if (error instanceof ApiConflictError) {
      errorMessage.value = `并发冲突：${error.message}`
    } else {
      errorMessage.value = error instanceof Error ? error.message : '采集结果校验操作失败'
    }
  } finally {
    actingId.value = null
    await reload()
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const payload = await fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}?${query.toString()}`)
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集结果校验列表读取失败'
  }
}

onMounted(reload)
</script>
