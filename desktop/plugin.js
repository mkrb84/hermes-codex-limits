import {
  Button,
  Popover,
  PopoverContent,
  PopoverTrigger,
  cn,
  haptic,
  host,
  useMutation,
  usePluginI18n,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'codex-limits'
const POLL_INTERVAL_MS = 300_000

export function findWindow(data, kind) {
  const needle = String(kind || '').toLowerCase()
  return (data?.windows || []).find(window =>
    String(window?.label || '').toLowerCase().includes(needle)
  ) || null
}

export function formatCompact(data) {
  const session = findWindow(data, 'session')?.remaining_percent
  const weekly = findWindow(data, 'weekly')?.remaining_percent
  const left = Number.isFinite(session) ? `${session}%` : '—'
  const right = Number.isFinite(weekly) ? `${weekly}%` : '—'
  return `Codex ${left} · ${right}`
}

export function statusTone(data) {
  const values = (data?.windows || [])
    .map(window => Number(window?.remaining_percent))
    .filter(Number.isFinite)
  if (!values.length) return 'text-(--ui-text-tertiary)'
  const lowest = Math.min(...values)
  if (lowest <= 10) return 'text-(--ui-danger)'
  if (lowest <= 30) return 'text-(--ui-accent)'
  return 'text-(--ui-text-tertiary)'
}

export function formatReset(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString([], {
    dateStyle: 'medium',
    timeStyle: 'short'
  })
}

function UsageBar({ window }) {
  const remaining = Math.max(0, Math.min(100, Number(window?.remaining_percent) || 0))
  const fill = remaining <= 10 ? 'bg-(--ui-danger)' : 'bg-(--ui-accent)'
  return jsxs('div', {
    className: 'space-y-1.5',
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between gap-4',
        children: [
          jsx('span', {
            className: 'text-xs font-medium',
            children: window?.displayLabel || window?.label || 'Usage'
          }),
          jsx('span', {
            className: 'font-mono text-xs tabular-nums',
            children: `${remaining}%`
          })
        ]
      }),
      jsx('div', {
        className: 'h-1.5 overflow-hidden rounded-full bg-(--ui-stroke-secondary)',
        children: jsx('div', {
          className: cn('h-full rounded-full transition-[width]', fill),
          style: { width: `${remaining}%` }
        })
      }),
      jsx('div', {
        className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
        children: window?.resetText || formatReset(window?.reset_at)
      })
    ]
  })
}

function LimitsPanel({ ctx, query, refresh, t }) {
  const data = query.data
  const session = findWindow(data, 'session')
  const weekly = findWindow(data, 'weekly')

  if (query.isPending && !data) {
    return jsx('div', {
      className: 'w-72 p-3 text-xs text-(--ui-text-tertiary)',
      children: t('loading')
    })
  }

  if (query.isError && !data) {
    return jsxs('div', {
      className: 'w-72 space-y-3 p-3',
      children: [
        jsx('div', { className: 'text-xs text-(--ui-danger)', children: t('unavailable') }),
        jsx(Button, {
          onClick: () => query.refetch(),
          size: 'sm',
          variant: 'outline',
          children: t('retry')
        })
      ]
    })
  }

  return jsxs('div', {
    className: 'w-72 space-y-3 p-3',
    children: [
      jsxs('div', {
        className: 'flex items-start justify-between gap-3',
        children: [
          jsxs('div', {
            children: [
              jsx('div', { className: 'text-sm font-medium', children: t('title') }),
              jsx('div', {
                className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
                children: data?.plan ? t('plan', data.plan) : 'OpenAI Codex'
              })
            ]
          }),
          jsx(Button, {
            disabled: query.isFetching || refresh.isPending,
            onClick: () => {
              haptic('tap')
              const route = activeRoute()
              if (route) refresh.mutate(route)
            },
            size: 'sm',
            variant: 'outline',
            children: refresh.isPending ? t('refreshing') : t('refresh')
          })
        ]
      }),
      session ? jsx(UsageBar, {
        window: {
          ...session,
          displayLabel: t('session'),
          resetText: t('resets', formatReset(session.reset_at))
        }
      }) : null,
      weekly ? jsx(UsageBar, {
        window: {
          ...weekly,
          displayLabel: t('weekly'),
          resetText: t('resets', formatReset(weekly.reset_at))
        }
      }) : null,
      jsxs('div', {
        className: 'border-t border-(--ui-stroke-secondary) pt-2 text-[0.6875rem] text-(--ui-text-tertiary)',
        children: [
          jsx('div', { children: t('banked', data?.banked_reset_count || 0) }),
          data?.stale ? jsx('div', {
            className: 'mt-1 text-(--ui-accent)',
            children: t('stale')
          }) : null,
          jsx('div', {
            className: 'mt-1 text-(--ui-text-quaternary)',
            children: t('noTokens')
          })
        ]
      })
    ]
  })
}

function activeRoute() {
  const connectionId = host.state.connectionId.get()
  const profile = host.state.profile.get()
  if (!connectionId || !profile) return null
  return { connectionId, profile }
}

function sameRoute(left, right) {
  return Boolean(
    left &&
    right &&
    left.connectionId === right.connectionId &&
    left.profile === right.profile
  )
}

async function requestUsage(ctx, expectedRoute, force = false) {
  const before = activeRoute()
  if (!sameRoute(before, expectedRoute)) {
    throw new Error('Codex limits active REST route changed before request')
  }

  const query = `profile=${encodeURIComponent(before.profile)}${force ? '&force=true' : ''}`
  let data
  let requestError
  let requestFailed = false
  try {
    data = await ctx.rest(`/usage?${query}`, { timeoutMs: 15_000 })
  } catch (error) {
    requestFailed = true
    requestError = error
  }

  const after = activeRoute()
  if (!sameRoute(before, after)) {
    throw new Error('Codex limits active REST route changed during request')
  }
  if (requestFailed) throw requestError
  return data
}

export function CodexLimitsChip({ ctx }) {
  const t = usePluginI18n(ID)
  const queryClient = useQueryClient()
  const connectionId = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  const scopeResolved = Boolean(connectionId && profile)
  const requestRoute = scopeResolved ? { connectionId, profile } : null
  const queryKey = [ctx.source, ID, 'usage', connectionId, profile]
  const query = useQuery({
    queryKey,
    queryFn: async () => {
      if (!requestRoute) throw new Error('Active Hermes REST route is unresolved')
      try {
        return await requestUsage(ctx, requestRoute)
      } catch (error) {
        queryClient.setQueryData(queryKey, null)
        throw error
      }
    },
    enabled: scopeResolved,
    gcTime: 0,
    refetchInterval: POLL_INTERVAL_MS,
    refetchIntervalInBackground: false,
    retry: 1,
    staleTime: 240_000
  })
  const refresh = useMutation({
    mutationFn: requestScope => requestUsage(ctx, requestScope, true),
    onMutate: requestScope => requestScope || activeRoute(),
    onSuccess: (data, requestScope, mutationScope) => {
      const captured = mutationScope || requestScope
      if (sameRoute(captured, activeRoute())) {
        queryClient.setQueryData(
          [ctx.source, ID, 'usage', captured.connectionId, captured.profile],
          data
        )
      }
    },
    onError: (_error, requestScope, mutationScope) => {
      const captured = mutationScope || requestScope
      if (!captured) return

      queryClient.setQueryData(
        [ctx.source, ID, 'usage', captured.connectionId, captured.profile],
        null
      )
      if (sameRoute(captured, activeRoute())) {
        host.notify({ kind: 'error', message: t('unavailable') })
      }
    }
  })

  const displayQuery = !scopeResolved
    ? { ...query, data: null, isError: true, isPending: false }
    : query

  const label = displayQuery.isPending && !displayQuery.data
    ? 'Codex …'
    : displayQuery.isError && !displayQuery.data
      ? 'Codex !'
      : formatCompact(displayQuery.data)

  return jsxs(Popover, {
    children: [
      jsx(PopoverTrigger, {
        asChild: true,
        children: jsx('button', {
          'aria-label': t('chipAria', label),
          className: cn(
            'inline-flex h-full items-center px-1.5 font-mono text-[0.6875rem] tabular-nums transition-colors',
            'hover:bg-(--chrome-action-hover) hover:text-foreground',
            displayQuery.isError && !displayQuery.data
              ? 'text-(--ui-danger)'
              : statusTone(displayQuery.data)
          ),
          title: t('chipTip'),
          type: 'button',
          children: label
        })
      }),
      jsx(PopoverContent, {
        align: 'end',
        className: 'w-auto p-0',
        side: 'top',
        children: jsx(LimitsPanel, { ctx, query: displayQuery, refresh, t })
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Codex Limits',
  defaultEnabled: false,
  register(ctx) {
    ctx.i18n.register({
      en: {
        title: 'Codex limits',
        plan: value => `Plan: ${value}`,
        session: '5-hour window',
        weekly: 'Weekly window',
        resets: value => `Resets ${value}`,
        banked: value => `Banked resets: ${value}`,
        refresh: 'Refresh',
        refreshing: 'Refreshing…',
        retry: 'Try again',
        loading: 'Loading Codex limits…',
        unavailable: 'Codex usage is temporarily unavailable',
        stale: 'Showing the last successful update',
        noTokens: 'Read-only usage check · no model tokens',
        chipTip: 'Codex subscription limits',
        chipAria: value => `Codex limits: ${value}`
      },
      ru: {
        title: 'Лимиты Codex',
        plan: value => `Тариф: ${value}`,
        session: 'Окно 5 часов',
        weekly: 'Недельное окно',
        resets: value => `Сброс: ${value}`,
        banked: value => `Сохранённых сбросов: ${value}`,
        refresh: 'Обновить',
        refreshing: 'Обновление…',
        retry: 'Повторить',
        loading: 'Получаю лимиты Codex…',
        unavailable: 'Лимиты Codex временно недоступны',
        stale: 'Показаны последние успешные данные',
        noTokens: 'Только чтение · токены модели не расходуются',
        chipTip: 'Лимиты подписки Codex',
        chipAria: value => `Лимиты Codex: ${value}`
      }
    })

    ctx.register({
      id: 'status',
      area: 'statusBar.right',
      order: 125,
      render: () => jsx(CodexLimitsChip, { ctx })
    })
  }
}
