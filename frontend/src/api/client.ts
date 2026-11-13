/** 统一请求封装：拼后端地址、抛网络错误、给页脚留一句可读的说明。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export class ApiConflictError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ApiConflictError'
  }
}

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  return fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}

/** 动作类请求的统一回执：200 业务拒绝与 409 并发冲突都转成可读消息。 */
export async function postAction(
  path: string,
  body: Record<string, unknown>,
): Promise<{ ok: boolean; message: string; entry: Record<string, unknown> | null }> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  const payload = (await response.json().catch(() => null)) as {
    ok?: boolean
    message?: string
    detail?: string
    entry?: Record<string, unknown> | null
  } | null
  if (response.status === 409) {
    throw new ApiConflictError(payload?.detail ?? '数据已被其他入口更新，请刷新后重试')
  }
  if (!response.ok || !payload) {
    throw new Error(`接口返回 ${response.status}，动作未生效`)
  }
  return {
    ok: Boolean(payload.ok),
    message: payload.message ?? '操作未生效',
    entry: payload.entry ?? null,
  }
}
