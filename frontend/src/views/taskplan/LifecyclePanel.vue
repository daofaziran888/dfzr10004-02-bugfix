<template>
  <div v-if="view" class="lifecycle-panel">
    <h3>计划生命周期视图</h3>
    <p class="panel-hint">
      当前阶段：<strong>{{ view.current_stage }}</strong>
      <span v-if="view.is_terminal" class="terminal-tag">已结案，历史记录可查看但不能重新启动</span>
    </p>

    <!-- 阶段流水线：只允许相邻流转，当前阶段之前为已完成、之后不可跳达 -->
    <ol class="stage-track">
      <li
        v-for="(stage, index) in view.stages"
        :key="stage"
        class="stage-node"
        :class="{
          'is-current': index === view.current_index,
          'is-past': index < view.current_index,
          'is-future': index > view.current_index,
        }"
      >
        <span class="stage-dot">{{ index + 1 }}</span>
        <span class="stage-name">{{ stage }}</span>
      </li>
    </ol>

    <!-- 每次变更都注明当前阶段、操作人与版本 -->
    <h4>变更轨迹</h4>
    <ul class="history-list">
      <li v-for="event in [...view.history].reverse()" :key="event.seq" class="history-item">
        <div class="history-head">
          <span class="history-action">{{ event.action }}</span>
          <span :class="stageClass(event.to_stage)">{{ event.to_stage }}</span>
          <span class="history-version">v{{ event.version }}</span>
        </div>
        <div class="history-meta">
          <span>{{ event.from_stage ? `${event.from_stage} → ${event.to_stage}` : `进入 ${event.to_stage}` }}</span>
          <span>{{ event.operator }}</span>
          <span>{{ formatTs(event.at) }}</span>
        </div>
        <div v-if="event.note" class="history-note">{{ event.note }}</div>
      </li>
    </ul>

    <h4>配额台账（暂停只冻结、不删除）</h4>
    <ul class="quota-list">
      <li v-for="record in view.quota_records" :key="record.seq" class="quota-item">
        <span class="quota-type">{{ record.type }}</span>
        <span class="quota-label">{{ record.label }}</span>
        <span class="quota-time">{{ formatTs(record.at) }}</span>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { formatTs, stageClass, type LifecycleView } from './lifecycle'

defineProps<{ view: LifecycleView | null }>()
</script>
