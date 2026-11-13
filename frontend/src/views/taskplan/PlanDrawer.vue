<template>
  <div v-if="view" class="drawer-mask" @click.self="$emit('close')">
    <aside class="drawer">
      <header class="drawer-head">
        <div>
          <h3>{{ view.entry['计划编号'] }} 计划详情</h3>
          <p class="panel-hint">
            当前阶段：<strong>{{ view.current_stage }}</strong>
            · 版本 v{{ view.entry.version }}
          </p>
        </div>
        <button class="btn ghost" type="button" @click="$emit('close')">关闭</button>
      </header>

      <dl class="detail-grid">
        <template v-for="field in detailFields" :key="field">
          <dt>{{ field }}</dt>
          <dd>{{ (view.entry[field] as string) ?? '—' }}</dd>
        </template>
        <dt>可执行动作</dt>
        <dd>
          <span v-if="!view.allowed_actions.length" class="muted">终态，无可用动作</span>
          <span v-for="action in view.allowed_actions" :key="action" class="action-chip">{{ action }}</span>
        </dd>
      </dl>

      <LifecyclePanel :view="view" />
    </aside>
  </div>
</template>

<script setup lang="ts">
import LifecyclePanel from './LifecyclePanel.vue'
import type { LifecycleView } from './lifecycle'

defineProps<{ view: LifecycleView | null }>()
defineEmits<{ (e: 'close'): void }>()

const detailFields = ['计划编号', '目标站点', '采集频率', '数据范围', '优先级', '资源配额', '预计用时', '计划状态']
</script>
