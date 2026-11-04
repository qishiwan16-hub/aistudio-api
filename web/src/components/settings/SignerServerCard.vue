<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { systemApi } from '@/api/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import type { SignerServerItem } from '@/types/system.ts'
import {
  Activity,
  Check,
  CheckCircle2,
  Edit2,
  Loader2,
  Plus,
  Trash2,
  XCircle,
} from 'lucide-vue-next'

const toast = useToastStore()
const loading = ref(false)
const saving = ref(false)
const switching = ref<string | null>(null)
const testing = ref<string | null>(null)
const activeId = ref('')
const servers = ref<SignerServerItem[]>([])
const showEditor = ref(false)
const editing = ref(false)
const form = ref<Partial<SignerServerItem>>({})
const headersText = ref('{}')
const results = ref<Record<string, { ok: boolean; detail: string }>>({})

async function load() {
  loading.value = true
  try {
    const result = await systemApi.getSignerServers()
    activeId.value = result.active_server
    servers.value = result.servers
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '无法读取 WAA 签名服务')
  } finally {
    loading.value = false
  }
}

function newServer() {
  editing.value = false
  form.value = {
    name: '',
    description: '',
    url: 'https://waa.spinsnow.fun',
    proxy_url: '',
    user_agent: '',
  }
  headersText.value = '{\n  "X-User-Agent": "grpc-web-javascript/0.1"\n}'
  showEditor.value = true
}

function editServer(server: SignerServerItem) {
  editing.value = true
  form.value = { ...server }
  headersText.value = JSON.stringify(server.headers || {}, null, 2)
  showEditor.value = true
}

async function save() {
  if (!form.value.name?.trim() || !form.value.url?.trim()) {
    toast.error('名称和服务地址必填')
    return
  }
  let headers: Record<string, string>
  try {
    const parsed: unknown = JSON.parse(headersText.value || '{}')
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error()
    headers = Object.fromEntries(Object.entries(parsed).map(([k, v]) => [k, String(v)]))
  } catch {
    toast.error('请求头必须是 JSON 对象')
    return
  }
  saving.value = true
  try {
    await systemApi.upsertSignerServer({
      ...form.value,
      name: form.value.name.trim(),
      url: form.value.url.trim(),
      proxy_url: form.value.proxy_url?.trim() || '',
      headers,
    })
    showEditor.value = false
    toast.success('签名服务已保存')
    await load()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    saving.value = false
  }
}

async function switchTo(server: SignerServerItem) {
  switching.value = server.id
  try {
    await systemApi.switchSignerServer(server.id)
    toast.success(`当前签名服务：${server.name}`)
    await load()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '切换失败')
  } finally {
    switching.value = null
  }
}

async function test(server: SignerServerItem) {
  testing.value = server.id
  try {
    const result = await systemApi.testSignerServer(server.url, server.proxy_url || '')
    results.value[server.id] = {
      ok: result.ok,
      detail: result.ok ? `${result.elapsed_ms} ms` : result.error || `HTTP ${result.status_code || '?'}`,
    }
    if (result.ok) toast.success(`${server.name} 连通正常`)
    else toast.error(`${server.name} 测试失败：${results.value[server.id].detail}`)
  } catch (error) {
    const detail = error instanceof Error ? error.message : '连接失败'
    results.value[server.id] = { ok: false, detail }
    toast.error(detail)
  } finally {
    testing.value = null
  }
}

async function remove(server: SignerServerItem) {
  if (server.id === activeId.value) {
    toast.error('请先切换到其他签名服务，再删除当前服务')
    return
  }
  if (!confirm(`删除签名服务“${server.name}”？`)) return
  try {
    await systemApi.deleteSignerServer(server.id)
    toast.success('签名服务已删除')
    await load()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '删除失败')
  }
}

onMounted(() => void load())
</script>

<template>
  <section class="space-y-4 border-t border-gray-200 pt-6">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h3 class="text-base font-semibold text-gray-900">WAA 签名服务</h3>
        <p class="mt-1 text-sm text-gray-500">配置签名节点、节点代理和与其匹配的请求特征。</p>
      </div>
      <button class="inline-flex items-center gap-2 rounded-md bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-700" @click="newServer">
        <Plus class="h-4 w-4" /> 添加服务
      </button>
    </header>

    <div v-if="loading" class="py-5 text-sm text-gray-500">正在读取签名服务…</div>
    <div v-else class="divide-y divide-gray-200 border-y border-gray-200">
      <article v-for="server in servers" :key="server.id" class="flex flex-col gap-3 py-4 md:flex-row md:items-center md:justify-between">
        <div class="min-w-0 space-y-1">
          <div class="flex flex-wrap items-center gap-2">
            <span class="font-medium text-gray-900">{{ server.name }}</span>
            <span v-if="server.id === activeId" class="inline-flex items-center gap-1 rounded bg-emerald-50 px-2 py-0.5 text-xs text-emerald-700"><Check class="h-3.5 w-3.5" />当前使用</span>
            <span v-if="results[server.id]" class="inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs" :class="results[server.id].ok ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-700'">
              <CheckCircle2 v-if="results[server.id].ok" class="h-3.5 w-3.5" />
              <XCircle v-else class="h-3.5 w-3.5" /> {{ results[server.id].detail }}
            </span>
          </div>
          <div class="break-all font-mono text-xs text-gray-600">{{ server.url }}</div>
          <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500">
            <span>节点代理：{{ server.proxy_url ? '已配置' : '直连' }}</span>
            <span v-if="server.user_agent">UA：{{ server.user_agent }}</span>
            <span v-if="server.description">{{ server.description }}</span>
          </div>
        </div>
        <div class="flex shrink-0 items-center gap-2">
          <button class="rounded p-2 text-gray-600 hover:bg-gray-100" title="测试连接" :disabled="testing === server.id" @click="test(server)">
            <Loader2 v-if="testing === server.id" class="h-4 w-4 animate-spin" />
            <Activity v-else class="h-4 w-4" />
          </button>
          <button v-if="server.id !== activeId" class="rounded border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50" :disabled="switching === server.id" @click="switchTo(server)">
            {{ switching === server.id ? '切换中…' : '切换' }}
          </button>
          <button class="rounded p-2 text-gray-600 hover:bg-gray-100" title="编辑" @click="editServer(server)"><Edit2 class="h-4 w-4" /></button>
          <button v-if="server.id !== activeId" class="rounded p-2 text-rose-600 hover:bg-rose-50" title="删除" @click="remove(server)"><Trash2 class="h-4 w-4" /></button>
        </div>
      </article>
      <div v-if="servers.length === 0" class="py-5 text-sm text-gray-500">尚未配置签名服务。</div>
    </div>

    <div v-if="showEditor" class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" @click.self="showEditor = false">
      <form class="max-h-[90vh] w-full max-w-xl space-y-4 overflow-y-auto rounded-lg bg-white p-5 shadow-xl" @submit.prevent="save">
        <div class="flex items-center justify-between border-b border-gray-200 pb-3">
          <h4 class="font-semibold text-gray-900">{{ editing ? '编辑签名服务' : '添加签名服务' }}</h4>
          <button type="button" class="rounded p-1 text-gray-500 hover:bg-gray-100" aria-label="关闭" @click="showEditor = false">×</button>
        </div>
        <label class="block space-y-1 text-sm"><span>名称</span><input v-model="form.name" required class="w-full rounded border border-gray-300 px-3 py-2" /></label>
        <label class="block space-y-1 text-sm"><span>服务 URL</span><input v-model="form.url" required placeholder="https://signer.example" class="w-full rounded border border-gray-300 px-3 py-2 font-mono" /></label>
        <label class="block space-y-1 text-sm"><span>代理 URL</span><input v-model="form.proxy_url" placeholder="http://user:pass@host:port 或 socks5://host:port" class="w-full rounded border border-gray-300 px-3 py-2 font-mono" /><small class="text-gray-500">留空继承全局代理；填写 direct 表示直连。本地 signer URL 会忽略代理。</small></label>
        <label class="block space-y-1 text-sm"><span>User-Agent</span><textarea v-model="form.user_agent" rows="2" class="w-full rounded border border-gray-300 px-3 py-2 font-mono text-xs" /></label>
        <label class="block space-y-1 text-sm"><span>自定义请求头（JSON）</span><textarea v-model="headersText" rows="4" class="w-full rounded border border-gray-300 px-3 py-2 font-mono text-xs" /></label>
        <label class="block space-y-1 text-sm"><span>说明</span><input v-model="form.description" class="w-full rounded border border-gray-300 px-3 py-2" /></label>
        <footer class="flex justify-end gap-2 border-t border-gray-200 pt-3">
          <button type="button" class="rounded border border-gray-300 px-3 py-2 text-sm" @click="showEditor = false">取消</button>
          <button type="submit" class="rounded bg-indigo-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50" :disabled="saving">{{ saving ? '保存中…' : '保存' }}</button>
        </footer>
      </form>
    </div>
  </section>
</template>
