<template>
  <section class="page" data-module="ventilation">
    <header class="page-head">
      <div>
        <h2>通风系统管理</h2>
        <p class="page-desc">
          每台通风机按额定风量定运行频率合理范围；实测风量低于额定五成落降频、低于三成判故障，
          两档边界不重叠，判定打架取更严一档。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记通风设备</button>
        <button class="btn" type="button" @click="exportRows">导出通风系统清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statsCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <nav class="filter-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="btn"
        :class="{ primary: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
      <span class="threshold-hint">
        当前阈值：故障 &lt; 额定风量的 {{ percent(currentThreshold['故障阈值']) }}，
        降频 &lt; {{ percent(currentThreshold['降频阈值']) }}
        （第 {{ currentThreshold['版本号'] }} 版，{{ currentThreshold['生效时间'] }} 生效）
      </span>
    </nav>

    <!-- 设备列表 -->
    <template v-if="activeTab === 'devices'">
      <form class="filter-bar" @submit.prevent="reload">
        <label class="filter-item">
          <span>设备编号</span>
          <input v-model="keyword" placeholder="按设备编号检索" />
        </label>
        <label class="filter-item">
          <span>设备状态</span>
          <select v-model="statusFilter">
            <option value="">全部</option>
            <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
          </select>
        </label>
        <button class="btn" type="submit">查询</button>
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
            <td>{{ row['设备编号'] ?? '—' }}</td>
            <td>{{ row['设备类型'] ?? '—' }}</td>
            <td>{{ row['额定风量'] ?? '—' }}</td>
            <td>{{ row['频率下限'] }}~{{ row['频率上限'] }} Hz</td>
            <td>{{ row['运行频率'] ?? '—' }}</td>
            <td>{{ row['实测风量'] ?? '—' }}</td>
            <td>{{ formatRatio(row) }}</td>
            <td>{{ row['所属巷道'] ?? '—' }}</td>
            <td>{{ row['上次检修'] ?? '—' }}</td>
            <td>{{ row.status }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="reportAirflow(row)">实测风量上报</button>
              <button
                  class="link"
                  type="button"
                  :disabled="row.status === '降频运行'"
                  :title="row.status === '降频运行' ? '已在降频，重复提交将被拒绝' : ''"
                  @click="runAction('降频运行', row)"
              >降频运行</button>
              <button class="link" type="button" @click="runAction('故障停机', row)">故障停机</button>
              <button class="link" type="button" @click="runAction('恢复正常', row)">恢复正常</button>
              <button class="link" type="button" @click="runAction('办理更换', row)">办理更换</button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length + 1" class="empty-state">暂无通风系统数据，可先登记通风设备</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span>共 {{ total }} 条通风系统记录</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>

    <!-- 欠风台账 -->
    <template v-else-if="activeTab === 'ledger'">
      <div class="filter-bar">
        <label class="filter-item">
          <span>日期（YYYY-MM-DD）</span>
          <input v-model="ledgerDay" placeholder="留空查看全部" />
        </label>
        <button class="btn" type="button" @click="reloadLedger">查询台账</button>
        <button class="btn primary" type="button" @click="closeYesterday">归档昨天台账</button>
        <span class="threshold-hint">归档后按当时阈值冻结，之后调整阈值不重算。</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>日期</th>
            <th>设备编号</th>
            <th>欠风分钟数</th>
            <th>归档阈值版本</th>
            <th>归档状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in ledgerRows" :key="String(row.id)">
            <td>{{ row['日期'] }}</td>
            <td>{{ row['设备编号'] }}</td>
            <td>{{ row['欠风分钟数'] }}</td>
            <td>
              第 {{ row['阈值版本号'] }} 版
              （故障&lt;{{ snapshotPercent(row, '故障阈值') }}，
              降频&lt;{{ snapshotPercent(row, '降频阈值') }}）
            </td>
            <td>{{ row['已归档'] ? '已冻结' : '当日累计中' }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="replay(row)">按当时阈值还原</button>
            </td>
          </tr>
          <tr v-if="!ledgerRows.length">
            <td colspan="6" class="empty-state">暂无欠风统计；设备进入降频/故障后按运行时段自动累计。</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span v-if="replayMessage" class="threshold-hint">{{ replayMessage }}</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>

    <!-- 阈值版本 -->
    <template v-else-if="activeTab === 'thresholds'">
      <table class="data-table">
        <thead>
          <tr>
            <th>版本号</th>
            <th>故障阈值（占额定风量）</th>
            <th>降频阈值（占额定风量）</th>
            <th>生效时间</th>
            <th>说明</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in thresholdVersions" :key="String(row.id)">
            <td>第 {{ row['版本号'] }} 版</td>
            <td>&lt; {{ percent(row['故障阈值']) }}</td>
            <td>&lt; {{ percent(row['降频阈值']) }}</td>
            <td>{{ row['生效时间'] }}</td>
            <td>{{ row['调整说明'] }}</td>
            <td>{{ row['当前生效'] ? '当前生效' : '历史版本（旧台账按它还原）' }}</td>
          </tr>
        </tbody>
      </table>
      <form class="filter-bar" style="margin-top: 12px" @submit.prevent="adjustThresholds">
        <label class="filter-item">
          <span>故障阈值（0~1，须严格小于降频阈值）</span>
          <input v-model="newFaultRatio" placeholder="例如 0.3" />
        </label>
        <label class="filter-item">
          <span>降频阈值（0~1）</span>
          <input v-model="newDerateRatio" placeholder="例如 0.5" />
        </label>
        <button class="btn primary" type="submit">发布新版本</button>
        <span class="threshold-hint">只影响之后的判定；已经归档的日子不重算。</span>
      </form>
      <footer class="page-foot">
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>

    <!-- 运行日志 -->
    <template v-else>
      <table class="data-table">
        <thead>
          <tr>
            <th>发生时刻</th>
            <th>设备编号</th>
            <th>动作</th>
            <th>前状态</th>
            <th>后状态</th>
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in logRows" :key="String(row.id)">
            <td>{{ row['发生时刻'] }}</td>
            <td>{{ row['设备编号'] ?? '平台' }}</td>
            <td>{{ row['动作'] }}</td>
            <td>{{ row['前状态'] ?? '—' }}</td>
            <td>{{ row['后状态'] ?? '—' }}</td>
            <td>{{ row['详情'] }}</td>
          </tr>
          <tr v-if="!logRows.length">
            <td colspan="6" class="empty-state">暂无运行日志。</td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- 登记设备 -->
    <div v-if="showCreate" class="modal-mask" @click.self="showCreate = false">
      <form class="modal-card" @submit.prevent="submitCreate">
        <h3>登记通风设备</h3>
        <p class="threshold-hint">额定风量必填且须为正数，否则不能保存；频率范围在 30~50Hz 内按设备收窄。</p>
        <label v-for="field in createFields" :key="field.prop" class="filter-item">
          <span>{{ field.label }}{{ field.required ? ' *' : '' }}</span>
          <input v-model="form[field.prop]" :placeholder="field.placeholder" />
        </label>
        <footer class="page-foot">
          <span v-if="createError" class="error-text">{{ createError }}</span>
          <span>
            <button class="btn" type="button" @click="showCreate = false">取消</button>
            <button class="btn primary" type="submit">保存</button>
          </span>
        </footer>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null | Record<string, unknown>>
type Threshold = Record<string, string | number>

const ENDPOINT = '/api/ventilation'
const columns = ['设备编号', '设备类型', '额定风量', '频率范围', '运行频率', '实测风量', '占额定风量', '所属巷道', '上次检修', '设备状态']
const statuses = ['正常', '降频运行', '故障停机', '已更换']
const tabs = [
  { key: 'devices', label: '设备列表' },
  { key: 'ledger', label: '欠风台账' },
  { key: 'thresholds', label: '阈值版本' },
  { key: 'logs', label: '运行日志' },
] as const

const createFields = [
  { prop: '设备编号', label: '设备编号', required: true, placeholder: 'VENT-0101' },
  { prop: '设备类型', label: '设备类型', required: true, placeholder: '主通风机 / 局部通风机' },
  { prop: '额定风量', label: '额定风量', required: true, placeholder: '正数，单位与现场一致（必填）' },
  { prop: '频率下限', label: '频率下限(Hz)', placeholder: '不低于 30，默认 30' },
  { prop: '频率上限', label: '频率上限(Hz)', placeholder: '不高于 50，默认 50' },
  { prop: '运行频率', label: '运行频率(Hz)', placeholder: '默认取下限' },
  { prop: '电流值', label: '电流值', placeholder: '如 86A' },
  { prop: '所属巷道', label: '所属巷道', placeholder: '如 主斜井' },
  { prop: '上次检修', label: '上次检修', placeholder: 'YYYY-MM-DD' },
  { prop: '运行时段', label: '运行时段', placeholder: '如 08:00-16:00，默认全天' },
]

const activeTab = ref<(typeof tabs)[number]['key']>('devices')
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')

const ledgerRows = ref<Row[]>([])
const ledgerDay = ref('')
const replayMessage = ref('')
const logRows = ref<Row[]>([])
const thresholdVersions = ref<Threshold[]>([])
const currentThreshold = ref<Threshold>({ 版本号: '-', 故障阈值: 0.3, 降频阈值: 0.5, 生效时间: '' })
const newFaultRatio = ref('0.3')
const newDerateRatio = ref('0.5')

const showCreate = ref(false)
const createError = ref('')
const form = reactive<Record<string, string>>({})

const statsCards = computed(() => [
  { label: '设备总数', value: total.value },
  { label: '正常设备', value: rows.value.filter((r) => r.status === '正常').length },
  { label: '降频设备', value: rows.value.filter((r) => r.status === '降频运行').length },
  { label: '故障设备', value: rows.value.filter((r) => r.status === '故障停机').length },
])

function percent(value: unknown): string {
  const number = Number(value)
  return Number.isFinite(number) ? `${Math.round(number * 100)}%` : '—'
}

function snapshotPercent(row: Row, key: string): string {
  const snapshot = row['阈值快照']
  return snapshot && typeof snapshot === 'object' ? percent((snapshot as Record<string, unknown>)[key]) : '—'
}

function formatRatio(row: Row): string {
  const measured = Number(row['实测风量'])
  const rated = Number(row['额定风量'])
  if (!measured || !rated) return '—'
  return percent(measured / rated)
}

async function parseError(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json()
    if (typeof body.detail === 'string') return body.detail
    if (typeof body.message === 'string') return body.message
  } catch {
    /* 非 JSON 错误体，用兜底文案 */
  }
  return fallback
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  Object.keys(form).forEach((key) => delete form[key])
  createError.value = ''
  showCreate.value = true
}

async function submitCreate() {
  createError.value = ''
  const values: Record<string, unknown> = {}
  for (const field of createFields) {
    const text = String(form[field.prop] ?? '').trim()
    if (!text) continue
    if (field.prop === '运行时段') {
      const match = /^(\d{1,2}:\d{2})\s*[-~]\s*(\d{1,2}:\d{2})$/.exec(text)
      if (!match) {
        createError.value = '运行时段格式应为 08:00-16:00'
        return
      }
      values['运行时段'] = [[match[1], match[2]]]
    } else {
      values[field.prop] = text
    }
  }
  try {
    const response = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values }) })
    const body = await response.json()
    if (!response.ok || body.ok === false) {
      createError.value = body.message || '登记失败'
      return
    }
    showCreate.value = false
    await reload()
  } catch (error) {
    createError.value = error instanceof Error ? error.message : '登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      errorMessage.value = await parseError(response, '动作未生效')
      return
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '通风系统操作失败'
  }
}

async function reportAirflow(row: Row) {
  errorMessage.value = ''
  const input = window.prompt(`录入「${row['设备编号']}」的实测风量（额定风量 ${row['额定风量']}）`)
  if (input === null) return
  const measured = Number(input)
  if (!Number.isFinite(measured) || measured < 0) {
    errorMessage.value = '实测风量必须是不小于 0 的数字'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/${row.id}/airflow`, {
      method: 'POST',
      body: JSON.stringify({ measured_airflow: measured }),
    })
    const body = await response.json().catch(() => null)
    if (!response.ok) {
      errorMessage.value = body?.detail || '实测风量上报失败'
      return
    }
    errorMessage.value = body.message
    await reload()
    if (activeTab.value === 'ledger') await reloadLedger()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '实测风量上报失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (keyword.value) params.set('keyword', keyword.value)
  if (statusFilter.value) params.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) throw new Error('通风设备列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '通风系统列表读取失败'
  }
}

async function reloadLedger() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (ledgerDay.value) params.set('day', ledgerDay.value)
  try {
    const response = await request(`${ENDPOINT}/ledger?${params.toString()}`)
    if (!response.ok) throw new Error('通风台账读取失败')
    ledgerRows.value = (await response.json()).items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '通风台账读取失败'
  }
}

async function closeYesterday() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/ledger/close`, { method: 'POST' })
    const body = await response.json().catch(() => null)
    if (!response.ok) {
      errorMessage.value = body?.detail || '归档失败'
      return
    }
    errorMessage.value = body.message
    await reloadLedger()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归档失败'
  }
}

async function replay(row: Row) {
  replayMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/ledger/${row.id}/replay`)
    const body = await response.json()
    replayMessage.value = body.message || '已按归档时阈值还原'
  } catch (error) {
    replayMessage.value = error instanceof Error ? error.message : '还原失败'
  }
}

async function reloadThresholds() {
  try {
    const response = await request(`${ENDPOINT}/thresholds`)
    if (!response.ok) throw new Error('阈值版本读取失败')
    const body = await response.json()
    thresholdVersions.value = body.versions ?? []
    currentThreshold.value = body.current
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '阈值版本读取失败'
  }
}

async function adjustThresholds() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/thresholds`, {
      method: 'PUT',
      body: JSON.stringify({
        fault_ratio: Number(newFaultRatio.value),
        derate_ratio: Number(newDerateRatio.value),
        reason: '值班页面人工调整',
      }),
    })
    const body = await response.json().catch(() => null)
    if (!response.ok || body?.ok === false) {
      errorMessage.value = body?.message || '阈值调整失败'
      return
    }
    errorMessage.value = body.message
    await reloadThresholds()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '阈值调整失败'
  }
}

async function reloadLogs() {
  try {
    const response = await request(`${ENDPOINT}/logs`)
    if (!response.ok) throw new Error('运行日志读取失败')
    logRows.value = (await response.json()).items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行日志读取失败'
  }
}

function switchTab(key: (typeof tabs)[number]['key']) {
  activeTab.value = key
  errorMessage.value = ''
  if (key === 'ledger') void reloadLedger()
  if (key === 'thresholds') void reloadThresholds()
  if (key === 'logs') void reloadLogs()
}

onMounted(() => {
  void reload()
  void reloadThresholds()
})
</script>

<style scoped>
.threshold-hint {
  font-size: 12px;
  color: var(--muted);
  align-self: center;
}
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}
.modal-card {
  background: #fff;
  border-radius: 10px;
  padding: 18px 20px;
  width: 460px;
  max-height: 86vh;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.link:disabled {
  color: #9ca3af;
  cursor: not-allowed;
}
</style>
