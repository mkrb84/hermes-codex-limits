import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'

const PLUGIN_URL = new URL('../desktop/plugin.js', import.meta.url)

async function synthetic(context, values) {
  const mod = new vm.SyntheticModule(Object.keys(values), function () {
    for (const [key, value] of Object.entries(values)) this.setExport(key, value)
  }, { context })
  await mod.evaluate()
  return mod
}

async function loadPlugin({ mutationState = {} } = {}) {
  const source = await readFile(PLUGIN_URL, 'utf8')
  const cacheWrites = []
  const hostState = {
    connectionId: 'connection-a',
    profile: 'profile-a',
    focusedConnectionId: 'connection-a',
    focusedProfile: 'profile-a'
  }
  const mutationOptions = []
  const queryOptions = []
  const context = vm.createContext({ console, Date, Intl, setTimeout, clearTimeout })
  const sdk = await synthetic(context, {
    Button: 'Button',
    Popover: 'Popover',
    PopoverContent: 'PopoverContent',
    PopoverTrigger: 'PopoverTrigger',
    cn: (...items) => items.filter(Boolean).join(' '),
    haptic: () => {},
    host: {
      notify: () => {},
      state: {
        connectionId: { get: () => hostState.connectionId },
        profile: { get: () => hostState.profile },
        focusedSessionOwner: {
          get: () => ({
            connectionId: hostState.focusedConnectionId,
            profile: hostState.focusedProfile
          })
        }
      }
    },
    useMutation: options => {
      mutationOptions.push(options)
      return { isError: false, isPending: false, mutate: () => {}, ...mutationState }
    },
    usePluginI18n: () => (key, value) => value === undefined ? key : `${key}:${value}`,
    useValue: atom => atom.get(),
    useQuery: options => {
      queryOptions.push(options)
      return {
        data: {
          plan: 'Team',
          windows: [
            { label: 'Session', remaining_percent: 94, reset_at: '2026-09-01T23:07:00+00:00' },
            { label: 'Weekly', remaining_percent: 91, reset_at: '2026-09-07T19:06:00+00:00' }
          ],
          banked_reset_count: 1
        },
        error: null,
        isError: false,
        isFetching: false,
        isPending: false
      }
    },
    useQueryClient: () => ({
      setQueryData: (key, data) => cacheWrites.push({ key, data })
    })
  })
  const jsxRuntime = await synthetic(context, {
    jsx: (type, props) => ({ type, props }),
    jsxs: (type, props) => ({ type, props })
  })
  const react = await synthetic(context, {})

  const mod = new vm.SourceTextModule(source, {
    context,
    identifier: PLUGIN_URL.href
  })
  await mod.link(specifier => {
    if (specifier === '@hermes/plugin-sdk') return sdk
    if (specifier === 'react/jsx-runtime') return jsxRuntime
    if (specifier === 'react') return react
    throw new Error(`unsupported import in plugin: ${specifier}`)
  })
  await mod.evaluate()
  return { cacheWrites, hostState, mutationOptions, namespace: mod.namespace, queryOptions, source }
}

test('compact label shows session and weekly remaining percentages', async () => {
  const { namespace } = await loadPlugin()
  const data = {
    windows: [
      { label: 'Session', remaining_percent: 94 },
      { label: 'Weekly', remaining_percent: 91 }
    ]
  }

  assert.equal(namespace.formatCompact(data), 'Codex 94% · 91%')
})

test('tone changes at warning and critical thresholds', async () => {
  const { namespace } = await loadPlugin()

  assert.match(namespace.statusTone({ windows: [{ remaining_percent: 31 }] }), /tertiary/)
  assert.match(namespace.statusTone({ windows: [{ remaining_percent: 30 }] }), /accent/)
  assert.match(namespace.statusTone({ windows: [{ remaining_percent: 10 }] }), /danger/)
})

test('plugin registers a right-side status bar contribution', async () => {
  const { namespace } = await loadPlugin()
  const contributions = []
  const ctx = {
    i18n: { register: () => {} },
    register: contribution => contributions.push(contribution),
    rest: async () => ({}),
    source: 'test-plugin'
  }

  namespace.default.register(ctx)

  assert.equal(namespace.default.id, 'codex-limits')
  assert.equal(contributions.length, 1)
  assert.equal(contributions[0].area, 'statusBar.right')
  assert.equal(typeof contributions[0].render, 'function')
})

test('status component polls every five minutes with hidden-renderer polling disabled', async () => {
  const { namespace, queryOptions } = await loadPlugin()
  const ctx = {
    rest: async () => ({}),
    source: 'test-plugin'
  }

  namespace.CodexLimitsChip({ ctx })

  assert.equal(queryOptions.length, 1)
  assert.equal(queryOptions[0].refetchInterval, 300_000)
  assert.equal(queryOptions[0].refetchIntervalInBackground, false)
})

test('status query cache is scoped to the active connection and profile', async () => {
  const { namespace, queryOptions } = await loadPlugin()
  const ctx = {
    rest: async () => ({}),
    source: 'test-plugin'
  }

  namespace.CodexLimitsChip({ ctx })

  assert.deepEqual(
    Array.from(queryOptions[0].queryKey),
    ['test-plugin', 'codex-limits', 'usage', 'connection-a', 'profile-a']
  )
  assert.equal(queryOptions[0].gcTime, 0)
})

test('a valid profile literally named unresolved remains enabled', async () => {
  const { hostState, namespace, queryOptions } = await loadPlugin()
  const ctx = {
    rest: async () => ({}),
    source: 'test-plugin'
  }
  hostState.profile = 'unresolved'

  namespace.CodexLimitsChip({ ctx })

  assert.equal(queryOptions[0].enabled, true)
  assert.deepEqual(
    Array.from(queryOptions[0].queryKey),
    ['test-plugin', 'codex-limits', 'usage', 'connection-a', 'unresolved']
  )
})

test('status query follows the active REST route rather than the focused session owner', async () => {
  const { hostState, namespace, queryOptions } = await loadPlugin()
  const calls = []
  const ctx = {
    rest: async path => {
      calls.push(path)
      return { plan: 'Active account', windows: [] }
    },
    source: 'test-plugin'
  }
  hostState.focusedConnectionId = 'focused-connection'
  hostState.focusedProfile = 'focused-profile'

  namespace.CodexLimitsChip({ ctx })
  const result = await queryOptions[0].queryFn()

  assert.equal(result.plan, 'Active account')
  assert.deepEqual(
    Array.from(queryOptions[0].queryKey),
    ['test-plugin', 'codex-limits', 'usage', 'connection-a', 'profile-a']
  )
  assert.deepEqual(calls, ['/usage?profile=profile-a'])
})

test('query rejects a response when the active REST route changes in flight', async () => {
  const { cacheWrites, hostState, namespace, queryOptions } = await loadPlugin()
  const ctx = {
    rest: async () => {
      hostState.connectionId = 'connection-b'
      hostState.profile = 'profile-b'
      return { plan: 'Account A', windows: [] }
    },
    source: 'test-plugin'
  }

  namespace.CodexLimitsChip({ ctx })

  await assert.rejects(queryOptions[0].queryFn(), /active REST route changed/)
  assert.equal(cacheWrites.length, 1)
  assert.deepEqual(
    Array.from(cacheWrites[0].key),
    ['test-plugin', 'codex-limits', 'usage', 'connection-a', 'profile-a']
  )
  assert.equal(cacheWrites[0].data, null)
})

test('failed refetch after an account switch clears only the new account cache', async () => {
  const { cacheWrites, hostState, namespace, queryOptions } = await loadPlugin()
  const failure = new Error('offline')
  let fail = false
  const ctx = {
    rest: async () => {
      if (fail) throw failure
      return { plan: 'Account A', windows: [] }
    },
    source: 'test-plugin'
  }

  namespace.CodexLimitsChip({ ctx })
  hostState.connectionId = 'connection-b'
  hostState.profile = 'profile-b'
  fail = true
  namespace.CodexLimitsChip({ ctx })

  await assert.rejects(queryOptions[1].queryFn(), failure)
  assert.equal(cacheWrites.length, 1)
  assert.deepEqual(
    Array.from(cacheWrites[0].key),
    ['test-plugin', 'codex-limits', 'usage', 'connection-b', 'profile-b']
  )
  assert.equal(cacheWrites[0].data, null)
})

test('failed forced refresh clears previously cached account data', async () => {
  const { cacheWrites, mutationOptions, namespace, queryOptions } = await loadPlugin()
  const ctx = {
    rest: async () => ({}),
    source: 'test-plugin'
  }

  namespace.CodexLimitsChip({ ctx })
  mutationOptions[0].onError(null, undefined, {
    connectionId: 'connection-a',
    profile: 'profile-a'
  })

  assert.equal(cacheWrites.length, 1)
  assert.deepEqual(Array.from(cacheWrites[0].key), Array.from(queryOptions[0].queryKey))
  assert.equal(cacheWrites[0].data, null)
})

test('forced refresh result cannot populate a different active account', async () => {
  const { cacheWrites, hostState, mutationOptions, namespace } = await loadPlugin()
  const accountA = { plan: 'Account A', windows: [] }
  const calls = []
  const ctx = {
    rest: async path => {
      calls.push(path)
      return accountA
    },
    source: 'test-plugin'
  }
  hostState.focusedConnectionId = 'focused-connection'
  hostState.focusedProfile = 'focused-profile'

  namespace.CodexLimitsChip({ ctx })
  const requestScope = mutationOptions[0].onMutate()
  const result = await mutationOptions[0].mutationFn(requestScope)
  assert.deepEqual(calls, ['/usage?profile=profile-a&force=true'])

  hostState.connectionId = 'connection-b'
  hostState.profile = 'profile-b'
  namespace.CodexLimitsChip({ ctx })
  mutationOptions[1].onSuccess(result, requestScope, requestScope)

  assert.equal(cacheWrites.length, 0)
})

test('a failed mutation does not hide query data after an account switch', async () => {
  const mutationState = {}
  const { cacheWrites, hostState, mutationOptions, namespace } = await loadPlugin({ mutationState })
  const ctx = {
    rest: async () => ({}),
    source: 'test-plugin'
  }

  namespace.CodexLimitsChip({ ctx })
  const requestScope = mutationOptions[0].onMutate()
  mutationOptions[0].onError(new Error('offline'), requestScope, requestScope)
  mutationState.isError = true
  hostState.connectionId = 'connection-b'
  hostState.profile = 'profile-b'
  const rendered = namespace.CodexLimitsChip({ ctx })
  const chipButton = rendered.props.children[0].props.children

  assert.deepEqual(
    Array.from(cacheWrites[0].key),
    ['test-plugin', 'codex-limits', 'usage', 'connection-a', 'profile-a']
  )
  assert.equal(cacheWrites[0].data, null)
  assert.equal(chipButton.props.children, 'Codex 94% · 91%')
})
