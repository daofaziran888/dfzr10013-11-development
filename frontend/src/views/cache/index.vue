<template>
  <section class="page" data-module="cache">
    <header class="page-head">
      <div>
        <h2>分析缓存管理管理</h2>
        <p class="page-desc">维护分析缓存的时效配置、失效策略与命中监控，为分析缓存管理提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记缓存策略</button>
        <button class="btn" type="button" @click="exportRows">导出分析缓存管理清单</button>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无分析缓存管理数据，可先登记缓存策略</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条分析缓存管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>

  <section class="page" data-module="cache-materialize">
    <header class="page-head">
      <div>
        <h2>分析物化</h2>
        <p class="page-desc">
          给缓存对象定义日期分区、分组维度与指标聚合含义，消费事实增量及撤销更正事件，
          输出可追溯的物化分区数据集，供多维下钻分析跨模块读取。
        </p>
      </div>
    </header>

    <div class="mat-grid">
      <article class="mat-card">
        <h3>物化口径定义</h3>
        <label class="mat-field">
          <span>策略编号</span>
          <input v-model="specForm.策略编号" placeholder="如 CACH-0001" />
        </label>
        <label class="mat-field">
          <span>日期分区字段</span>
          <input v-model="specForm.日期分区字段" placeholder="业务日期" />
        </label>
        <label class="mat-field">
          <span>分组维度（逗号分隔）</span>
          <input v-model="specForm.分组维度" placeholder="渠道,地区" />
        </label>
        <div class="mat-metric-head">
          <span>指标聚合含义</span>
          <button class="link" type="button" @click="addMetric">添加指标</button>
        </div>
        <div v-for="(metric, index) in specForm.指标" :key="index" class="mat-metric-row">
          <input v-model="metric.名称" placeholder="指标名称" />
          <select v-model="metric.聚合">
            <option value="count">计数</option>
            <option value="sum">求和</option>
            <option value="distinct_count">去重人数</option>
          </select>
          <input v-model="metric.来源字段" placeholder="来源字段（计数可空）" />
          <button class="link" type="button" @click="specForm.指标.splice(index, 1)">移除</button>
        </div>
        <div class="mat-actions">
          <button class="btn primary" type="button" @click="saveSpec">保存物化口径</button>
        </div>
      </article>

      <article class="mat-card">
        <h3>事实事件摄入</h3>
        <p class="mat-hint">
          事件携带事实编号、修订号、删除标记；更正会在同一次提交里成套完成「旧分区撤回 + 新分区加入」。
        </p>
        <textarea
          v-model="eventsText"
          rows="9"
          class="mat-textarea"
          placeholder='[{"事实编号":"F-1","修订号":1,"业务日期":"2026-09-01","维度":{"渠道":"线上"},"指标值":{"金额":100,"客户编号":"C-1"}}]'
        ></textarea>
        <div class="mat-actions">
          <button class="btn" type="button" @click="fillSampleEvents">填入示例事件</button>
          <button class="btn primary" type="button" @click="submitEvents">提交事件批次</button>
        </div>
        <pre v-if="ingestReport" class="mat-report">{{ ingestReport }}</pre>
      </article>
    </div>

    <article class="mat-card">
      <div class="mat-dataset-head">
        <h3>物化分区数据集</h3>
        <label class="mat-field inline">
          <span>缓存对象</span>
          <select v-model="selectedObject" @change="loadDataset">
            <option value="">请选择</option>
            <option v-for="item in matObjects" :key="item.策略编号" :value="item.策略编号">
              {{ item.策略编号 }}（水位 {{ item.水位 }}<template v-if="item.受污染分区数">，{{ item.受污染分区数 }} 个分区受污染</template>）
            </option>
          </select>
        </label>
        <div class="mat-actions">
          <button class="btn" type="button" :disabled="!selectedObject" @click="verifyDataset">校验一致性</button>
          <button class="btn" type="button" :disabled="!selectedObject" @click="rebuildDataset">重建（重放日志）</button>
        </div>
      </div>
      <p v-if="dataset" class="mat-hint">
        水位 {{ dataset.水位 }} · 修订集合摘要 {{ dataset.修订集合摘要 }} ·
        指标定义：{{ dataset.指标定义.map((m: any) => `${m.名称}=${m.聚合含义}`).join('、') }}
      </p>
      <table v-if="dataset" class="data-table">
        <thead>
          <tr>
            <th>分区键</th>
            <th>状态</th>
            <th>水位</th>
            <th>分区合计</th>
            <th>贡献数</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="partition in dataset.分区" :key="partition.分区键">
            <td>{{ partition.分区键 }}</td>
            <td>
              <span :class="partition.状态 === '受污染' ? 'tag-tainted' : 'tag-ok'">{{ partition.状态 }}</span>
              <div v-if="partition.污染原因.length" class="mat-taint">{{ partition.污染原因.join('；') }}</div>
            </td>
            <td>{{ partition.水位 }}</td>
            <td>{{ formatMetrics(partition.合计) }}</td>
            <td>{{ partition.贡献数 }}</td>
            <td>
              <button class="link" type="button" @click="loadContributions(partition.分区键)">贡献明细</button>
            </td>
          </tr>
          <tr v-if="!dataset.分区.length">
            <td colspan="6" class="empty-state">暂无物化分区，可先提交事实事件</td>
          </tr>
        </tbody>
      </table>
      <pre v-if="verifyReport" class="mat-report">{{ verifyReport }}</pre>
    </article>

    <article v-if="contributionView" class="mat-card">
      <h3>原始贡献明细 — 分区 {{ contributionView.分区键 }}</h3>
      <p v-if="contributionView.命中 === false" class="error-text">
        分区受污染，以下明细不作为命中结果：{{ contributionView.原因 }}
      </p>
      <table class="data-table">
        <thead>
          <tr>
            <th>事实编号</th>
            <th>修订号</th>
            <th>维度</th>
            <th>计数贡献</th>
            <th>求和贡献</th>
            <th>去重成员</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in contributionView.贡献" :key="row.事实编号">
            <td>{{ row.事实编号 }}</td>
            <td>{{ row.修订号 }}</td>
            <td>{{ formatDims(row.维度) }}</td>
            <td>{{ row.计数贡献 }}</td>
            <td>{{ formatMetrics(row.求和贡献) }}</td>
            <td>{{ formatMembers(row.去重成员) }}</td>
          </tr>
          <tr v-if="!contributionView.贡献.length">
            <td colspan="6" class="empty-state">该分区暂无存活贡献</td>
          </tr>
        </tbody>
      </table>
    </article>

    <footer class="page-foot">
      <span v-if="matMessage">{{ matMessage }}</span>
      <span v-if="matError" class="error-text">{{ matError }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/cache'
const columns = ["策略编号", "缓存对象", "缓存时间", "失效条件", "预加载", "容量上限", "命中统计", "策略状态"]
const actions = ["启用缓存", "暂停缓存", "关闭缓存"]
const statuses = ["草稿", "已启用", "已暂停", "已关闭"]
const stats = [{"label": "缓存策略总数", "value": 0}, {"label": "已启用缓存", "value": 0}, {"label": "已暂停缓存", "value": 0}]

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
  errorMessage.value = '缓存策略登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('分析缓存管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分析缓存管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('缓存策略列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分析缓存管理列表读取失败'
  }
}

/* ---------------------------------------------------------------- 分析物化 */

const specForm = ref({
  策略编号: 'CACH-0001',
  日期分区字段: '业务日期',
  分组维度: '渠道,地区',
  指标: [
    { 名称: '订单数', 聚合: 'count', 来源字段: '' },
    { 名称: '销售额', 聚合: 'sum', 来源字段: '金额' },
    { 名称: '去重客户数', 聚合: 'distinct_count', 来源字段: '客户编号' },
  ] as Array<{ 名称: string; 聚合: string; 来源字段: string }>,
})
const eventsText = ref('')
const ingestReport = ref('')
const matObjects = ref<any[]>([])
const selectedObject = ref('')
const dataset = ref<any>(null)
const contributionView = ref<any>(null)
const verifyReport = ref('')
const matMessage = ref('')
const matError = ref('')

function addMetric() {
  specForm.value.指标.push({ 名称: '', 聚合: 'count', 来源字段: '' })
}

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

async function readPayload(response: Response): Promise<any> {
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload?.detail ?? payload?.message ?? `接口返回 ${response.status}`)
  }
  return payload
}

async function refreshObjects(keepSelection = true) {
  const response = await request(`${ENDPOINT}/materialize/objects`)
  const payload = await readPayload(response)
  matObjects.value = payload.items ?? []
  if (!keepSelection || !matObjects.value.some((item) => item.策略编号 === selectedObject.value)) {
    selectedObject.value = matObjects.value[0]?.策略编号 ?? ''
  }
}

async function saveSpec() {
  matError.value = ''
  matMessage.value = ''
  const values = {
    策略编号: specForm.value.策略编号.trim(),
    日期分区字段: specForm.value.日期分区字段.trim() || '业务日期',
    分组维度: specForm.value.分组维度.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
    指标: specForm.value.指标.map((item) => ({
      名称: item.名称.trim(),
      聚合: item.聚合,
      来源字段: item.来源字段.trim(),
    })),
  }
  try {
    const response = await request(`${ENDPOINT}/materialize/define`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await readPayload(response)
    if (!payload.ok) {
      throw new Error(payload.message)
    }
    matMessage.value = payload.message
    await refreshObjects(false)
    selectedObject.value = values.策略编号
    await loadDataset()
  } catch (error) {
    matError.value = error instanceof Error ? error.message : '物化口径保存失败'
  }
}

function fillSampleEvents() {
  eventsText.value = JSON.stringify(
    [
      { 事实编号: 'F-2001', 修订号: 1, 业务日期: '2026-09-05', 维度: { 渠道: '线上', 地区: '华东' }, 指标值: { 金额: 66.5, 客户编号: 'C-08' } },
      { 事实编号: 'F-2001', 修订号: 2, 业务日期: '2026-09-06', 维度: { 渠道: '线下', 地区: '华东' }, 指标值: { 金额: 66.5, 客户编号: 'C-08' } },
    ],
    null,
    2,
  )
}

async function submitEvents() {
  matError.value = ''
  matMessage.value = ''
  ingestReport.value = ''
  if (!selectedObject.value) {
    matError.value = '请先保存物化口径或选择缓存对象'
    return
  }
  let events: unknown
  try {
    events = JSON.parse(eventsText.value || '[]')
  } catch {
    matError.value = '事件批次不是合法的 JSON 数组'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/materialize/${selectedObject.value}/events`, {
      method: 'POST',
      body: JSON.stringify({ values: { 事件: events } }),
    })
    const payload = await readPayload(response)
    if (!payload.ok) {
      throw new Error(payload.message)
    }
    const report = payload.entry ?? {}
    ingestReport.value = [
      `应用 ${report.应用} 条，跳过重复 ${report.跳过重复} 条，水位 ${report.水位}`,
      ...(report.变更集 ?? []).map((item: any) => `  [提交${item.提交序号}] ${item.变更} ${item.分区} ${item.维度键}（${item.事实编号} 修订${item.修订号}）`),
      ...(report.受污染分区 ?? []).map((item: any) => `  受污染分区 ${item.分区}：${item.原因.join('；')}`),
    ].join('\n')
    matMessage.value = payload.message
    await Promise.all([refreshObjects(), loadDataset()])
  } catch (error) {
    matError.value = error instanceof Error ? error.message : '事件批次提交失败'
  }
}

async function loadDataset() {
  matError.value = ''
  verifyReport.value = ''
  contributionView.value = null
  dataset.value = null
  if (!selectedObject.value) return
  try {
    const response = await request(`${ENDPOINT}/materialize/${selectedObject.value}/dataset`)
    dataset.value = await readPayload(response)
  } catch (error) {
    matError.value = error instanceof Error ? error.message : '物化数据集读取失败'
  }
}

async function loadContributions(partitionKey: string) {
  matError.value = ''
  try {
    const response = await request(`${ENDPOINT}/materialize/${selectedObject.value}/partitions/${partitionKey}/contributions`)
    contributionView.value = await readPayload(response)
  } catch (error) {
    matError.value = error instanceof Error ? error.message : '贡献明细读取失败'
  }
}

async function verifyDataset() {
  matError.value = ''
  verifyReport.value = ''
  try {
    const response = await request(`${ENDPOINT}/materialize/${selectedObject.value}/verify`)
    const payload = await readPayload(response)
    verifyReport.value = payload.一致
      ? `核对通过：增量物化结果与同一事实集合的全量聚合一致（核对事实 ${payload.核对事实数} 条，水位 ${payload.水位}）`
      : `核对不一致：\n${(payload.差异 ?? []).join('\n')}`
  } catch (error) {
    matError.value = error instanceof Error ? error.message : '一致性核对失败'
  }
}

async function rebuildDataset() {
  matError.value = ''
  matMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/materialize/${selectedObject.value}/rebuild`, {
      method: 'POST',
      body: JSON.stringify({ values: {} }),
    })
    const payload = await readPayload(response)
    if (!payload.ok) {
      throw new Error(payload.message)
    }
    matMessage.value = `${payload.message}：${payload.entry?.说明 ?? ''}`
    await Promise.all([refreshObjects(), loadDataset()])
  } catch (error) {
    matError.value = error instanceof Error ? error.message : '物化结果重建失败'
  }
}

onMounted(async () => {
  await reload()
  try {
    await refreshObjects()
    await loadDataset()
  } catch {
    /* 物化对象尚未定义时页面其余部分照常可用 */
  }
})
</script>

<style scoped>
.mat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}
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
.mat-field {
  display: block;
  margin-bottom: 8px;
}
.mat-field span {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 2px;
}
.mat-field.inline {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 0;
}
.mat-field input,
.mat-field select,
.mat-metric-row input,
.mat-metric-row select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 13px;
  width: 100%;
}
.mat-metric-head {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 4px;
}
.mat-metric-row {
  display: grid;
  grid-template-columns: 1fr 110px 1fr auto;
  gap: 6px;
  align-items: center;
  margin-bottom: 6px;
}
.mat-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.mat-hint {
  font-size: 12px;
  color: var(--muted);
  margin: 4px 0 8px;
}
.mat-textarea {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
  font-family: ui-monospace, monospace;
  font-size: 12px;
}
.mat-report {
  background: #0f172a;
  color: #e2e8f0;
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
  white-space: pre-wrap;
  margin-top: 8px;
}
.mat-dataset-head {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.mat-dataset-head h3 {
  margin: 0;
}
.mat-taint {
  font-size: 12px;
  color: #b42318;
}
.tag-ok {
  color: #067647;
}
.tag-tainted {
  color: #b42318;
  font-weight: 600;
}
</style>
