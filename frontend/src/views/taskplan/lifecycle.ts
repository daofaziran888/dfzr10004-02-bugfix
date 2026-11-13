/** 生命周期共享类型与展示工具，列表页、详情抽屉、校验清单共用同一口径。 */

export interface HistoryEvent {
  seq: number
  action: string
  from_stage: string | null
  to_stage: string
  at: number
  operator: string
  note: string
  version: number
}

export interface QuotaRecord {
  seq: number
  type: string
  label: string
  at: number
  version: number
}

export interface LifecycleView {
  entry: PlanRow
  stages: string[]
  current_stage: string
  current_index: number
  allowed_actions: string[]
  is_terminal: boolean
  history: HistoryEvent[]
  quota_records: QuotaRecord[]
}

export interface PlanRow {
  id: number
  status: string
  计划状态: string
  allowed_actions: string[]
  version: number
  pending: boolean
  abnormal: boolean
  [key: string]: string | number | boolean | null | string[] | HistoryEvent[] | QuotaRecord[]
}

export function formatTs(ts: number | undefined): string {
  if (!ts) return '—'
  // 后端给的是毫秒级 UTC 时间戳，按本地时区展示。
  return new Date(ts).toLocaleString('zh-CN', { hour12: false })
}

/** 阶段徽章配色：终态灰、暂停琥珀、执行蓝、待执行靛、草稿中性。 */
export function stageClass(stage: string): string {
  if (stage === '执行中' || stage === '运行中' || stage === '校验中') return 'stage stage-running'
  if (stage === '已暂停' || stage === '待复核') return 'stage stage-paused'
  if (stage === '已归档' || stage === '已完成') return 'stage stage-archived'
  if (stage === '待执行' || stage === '待处理') return 'stage stage-pending'
  if (stage === '校验通过') return 'stage stage-passed'
  if (stage === '校验不通过') return 'stage stage-failed'
  return 'stage stage-draft'
}
