<template>
  <section class="page" data-module="drill">
    <header class="page-head">
      <div>
        <h2>多维下钻分析管理</h2>
        <p class="page-desc">维护多维下钻的维度选择、层级递进与筛选组合，为多维下钻分析提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记下钻路径</button>
        <button class="btn" type="button" @click="exportRows">导出多维下钻分析清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无多维下钻分析数据，可先登记下钻路径</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条多维下钻分析记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>

  <section class="page" data-module="drill-materialize">
    <header class="page-head">
      <div>
        <h2>物化数据集消费</h2>
        <p class="page-desc">
          跨模块读取分析缓存管理的物化分区数据集：父级结果与叶级贡献来自同一修订集合，
          每次读取记录分区水位与指标定义作为凭证。
        </p>
      </div>
    </header>

    <article class="mat-card">
      <div class="consume-form">
        <label class="mat-field">
          <span>下钻路径</span>
          <select v-model="selectedPathId">
            <option v-for="row in rows" :key="String(row.id)" :value="Number(row.id)">
              {{ row.路径编号 }}（{{ row.起点指标 }}）
            </option>
          </select>
        </label>
        <label class="mat-field">
          <span>物化对象（策略编号）</span>
          <input v-model="consumeObject" placeholder="如 CACH-0001" />
        </label>
        <label class="mat-field">
          <span>分区键（留空读全部分区）</span>
          <input v-model="consumePartition" placeholder="如 2026-09-01" />
        </label>
        <div class="mat-actions">
          <button class="btn primary" type="button" @click="consume">读取分区数据集</button>
        </div>
      </div>
      <p v-if="consumeMessage" class="mat-hint">{{ consumeMessage }}</p>
    </article>

    <article v-if="consumeResult" class="mat-card">
      <h3>读取凭证 — {{ consumeResult.读取凭证.读取编号 }}</h3>
      <div class="receipt-grid">
        <div><span class="receipt-label">水位</span>{{ consumeResult.读取凭证.水位 }}</div>
        <div>
          <span class="receipt-label">分区水位</span>
          {{ formatPartitionWatermarks(consumeResult.读取凭证.分区水位) }}
        </div>
        <div>
          <span class="receipt-label">指标定义</span>
          {{ consumeResult.读取凭证.指标定义.map((m: any) => `${m.名称}=${m.聚合含义}`).join('、') }}
        </div>
        <div><span class="receipt-label">修订集合摘要</span>{{ consumeResult.读取凭证.修订集合摘要 }}（{{ revisionCount }} 条事实）</div>
        <div>
          <span class="receipt-label">叶级与父级同源</span>
          <span :class="consumeResult.读取凭证.叶级与父级同源 ? 'tag-ok' : 'tag-tainted'">
            {{ consumeResult.读取凭证.叶级与父级同源 ? '是' : '否' }}
          </span>
        </div>
        <div>
          <span class="receipt-label">可靠</span>
          <span :class="consumeResult.读取凭证.可靠 ? 'tag-ok' : 'tag-tainted'">
            {{ consumeResult.读取凭证.可靠 ? '是' : `否（${consumeResult.读取凭证.受污染分区.join('、')} 受污染）` }}
          </span>
        </div>
        <div><span class="receipt-label">读取时间</span>{{ consumeResult.读取凭证.读取时间 }}</div>
      </div>
    </article>

    <article v-if="consumeResult" class="mat-card">
      <h3>父级结果（分区 × 维度分组）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>分区键</th>
            <th>维度</th>
            <th>指标</th>
            <th>贡献数</th>
            <th>状态</th>
            <th>水位</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in consumeResult.父级结果" :key="index">
            <td>{{ row.分区键 }}</td>
            <td>{{ formatDims(row.维度) }}</td>
            <td>{{ formatMetrics(row.指标) }}</td>
            <td>{{ row.贡献数 }}</td>
            <td><span :class="row.状态 === '受污染' ? 'tag-tainted' : 'tag-ok'">{{ row.状态 }}</span></td>
            <td>{{ row.水位 }}</td>
          </tr>
          <tr v-if="!consumeResult.父级结果.length">
            <td colspan="6" class="empty-state">所选范围内暂无聚合结果</td>
          </tr>
        </tbody>
      </table>
    </article>

    <article v-if="consumeResult" class="mat-card">
      <h3>叶级贡献（原始贡献明细）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>分区键</th>
            <th>事实编号</th>
            <th>修订号</th>
            <th>维度</th>
            <th>计数贡献</th>
            <th>求和贡献</th>
            <th>去重成员</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in consumeResult.叶级贡献" :key="`${row.分区键}-${row.事实编号}`">
            <td>{{ row.分区键 }}</td>
            <td>{{ row.事实编号 }}</td>
            <td>{{ row.修订号 }}</td>
            <td>{{ formatDims(row.维度) }}</td>
            <td>{{ row.计数贡献 }}</td>
            <td>{{ formatMetrics(row.求和贡献) }}</td>
            <td>{{ formatMembers(row.去重成员) }}</td>
          </tr>
          <tr v-if="!consumeResult.叶级贡献.length">
            <td colspan="7" class="empty-state">所选范围内暂无贡献明细</td>
          </tr>
        </tbody>
      </table>
    </article>

    <article v-if="readHistory.length" class="mat-card">
      <h3>历史读取记录</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>读取编号</th>
            <th>物化对象</th>
            <th>分区范围</th>
            <th>水位</th>
            <th>修订集合摘要</th>
            <th>同源</th>
            <th>可靠</th>
            <th>读取时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="record in readHistory" :key="record.读取编号">
            <td>{{ record.读取编号 }}</td>
            <td>{{ record.策略编号 }}</td>
            <td>{{ record.分区范围 }}</td>
            <td>{{ record.水位 }}</td>
            <td>{{ record.修订集合摘要 }}</td>
            <td>{{ record.叶级与父级同源 ? '是' : '否' }}</td>
            <td>{{ record.可靠 ? '是' : '否' }}</td>
            <td>{{ record.读取时间 }}</td>
          </tr>
        </tbody>
      </table>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/drill'
const columns = ["路径编号", "起点指标", "下钻维度", "下钻层级", "筛选条件", "结果展示", "分析时间", "路径状态"]
const actions = ["开始下钻", "完成分析", "归档路径"]
const statuses = ["已创建", "分析中", "已完成", "已归档"]
const stats = [{"label": "下钻路径总数", "value": 0}, {"label": "已完成分析", "value": 0}, {"label": "已归档分析", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '下钻路径登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('多维下钻分析动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '多维下钻分析操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('下钻路径列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (!selectedPathId.value && rows.value.length) {
      selectedPathId.value = Number(rows.value[0].id)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '多维下钻分析列表读取失败'
  }
}

/* ---------------------------------------------------------------- 物化数据集消费 */

const selectedPathId = ref<number | null>(null)
const consumeObject = ref('CACH-0001')
const consumePartition = ref('')
const consumeResult = ref<any>(null)
const consumeMessage = ref('')
const readHistory = ref<any[]>([])

const revisionCount = computed(() => Object.keys(consumeResult.value?.读取凭证?.修订集合 ?? {}).length)

function formatMetrics(metrics: Record<string, unknown>): string {
  const entries = Object.entries(metrics ?? {})
  if (!entries.length) return '—'
  return entries.map(([key, value]) => `${key}=${value}`).join(' · ')
}

function formatDims(dims: Record<string, unknown>): string {
  const entries = Object.entries(dims ?? {})
  if (!entries.length) return '（全部）'
  return entries.map(([key, value]) => `${key}=${value}`).join('，')
}

function formatMembers(members: Record<string, string[]>): string {
  const entries = Object.entries(members ?? {})
  if (!entries.length) return '—'
  return entries.map(([key, value]) => `${key}: ${(value ?? []).join('、')}`).join('；')
}

function formatPartitionWatermarks(watermarks: Record<string, number>): string {
  const entries = Object.entries(watermarks ?? {})
  if (!entries.length) return '—'
  return entries.map(([key, value]) => `${key}@${value}`).join('，')
}

async function consume() {
  errorMessage.value = ''
  consumeMessage.value = ''
  consumeResult.value = null
  if (!selectedPathId.value) {
    errorMessage.value = '请先选择下钻路径'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/${selectedPathId.value}/consume`, {
      method: 'POST',
      body: JSON.stringify({
        values: { 策略编号: consumeObject.value.trim(), 分区键: consumePartition.value.trim() },
      }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '物化数据集读取失败')
    }
    consumeResult.value = payload.entry
    consumeMessage.value = payload.message
    await loadReadHistory()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '物化数据集读取失败'
  }
}

async function loadReadHistory() {
  if (!selectedPathId.value) {
    readHistory.value = []
    return
  }
  try {
    const response = await request(`${ENDPOINT}/${selectedPathId.value}`)
    if (!response.ok) return
    const payload = await response.json()
    readHistory.value = payload.物化读取记录 ?? []
  } catch {
    /* 历史记录读取失败不影响主流程 */
  }
}

watch(selectedPathId, () => {
  consumeResult.value = null
  void loadReadHistory()
})

onMounted(async () => {
  await reload()
  await loadReadHistory()
})
</script>

<style scoped>
.mat-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 12px;
}
.mat-card h3 {
  margin: 0 0 8px;
  font-size: 14px;
}
.consume-form {
  display: flex;
  gap: 16px;
  align-items: flex-end;
  flex-wrap: wrap;
}
.mat-field span {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 2px;
}
.mat-field input,
.mat-field select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 13px;
  min-width: 200px;
}
.mat-actions {
  display: flex;
  gap: 8px;
}
.mat-hint {
  font-size: 12px;
  color: var(--muted);
  margin: 8px 0 0;
}
.receipt-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 8px;
  font-size: 13px;
}
.receipt-label {
  display: block;
  font-size: 12px;
  color: var(--muted);
}
.tag-ok {
  color: #067647;
}
.tag-tainted {
  color: #b42318;
  font-weight: 600;
}
</style>
