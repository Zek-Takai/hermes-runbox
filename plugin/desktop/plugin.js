// runbox Desktop half: the Runbox tab (beside Terminal).
// Shows one chat's box at a time: it follows the focused chat, or the chat picked in the header.
// Run / Check / Test / Stop call the same backend actions as the agent's tool. Files open in a
// main-workspace tab with line numbers and Edit/Save. Each run expands to its issue list and
// output; "Send to chat" puts the result into that chat's composer.
import { Button, ConfirmDialog, EmptyState, Input, host, useValue } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

let rest = null
const refreshListeners = new Set()
const notifyRefresh = () => refreshListeners.forEach((fn) => fn())

const OK_COLOR = '#3fb950'
const FAIL_COLOR = '#f85149'
const tint = (pct) => `color-mix(in srgb, var(--ui-accent) ${pct}%, transparent)`

const fileUrl = (dir, path) => `/sessions/${encodeURIComponent(dir)}/file?path=${encodeURIComponent(path)}`

function fmtBytes(n) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function fmtAgo(seconds) {
  const s = Math.max(0, Date.now() / 1000 - seconds)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  return new Date(seconds * 1000).toLocaleDateString()
}

function renderIssue(i) {
  let where = i.file || '?'
  if (i.line) where += `:${i.line}${i.col ? `:${i.col}` : ''}`
  return `${where}: ${i.code ? `${i.code} ` : ''}${i.message || ''}`.trim()
}

async function loadTitles() {
  const titles = new Map()
  for (const scope of ['live', 'stored']) {
    try {
      const res = await host.request('session.list', { scope, limit: 200, include_hidden: true })
      for (const row of res?.sessions || []) {
        for (const id of [row.id, row.session_id]) if (id && !titles.has(id)) titles.set(id, row.title || '')
      }
    } catch {
      // titles are cosmetic
    }
  }
  return titles
}

// --- file viewer (main workspace tab) ----------------------------------------------------------

function FileView({ dir, path, line }) {
  const [file, setFile] = useState(null)
  const [draft, setDraft] = useState(null)
  const [error, setError] = useState('')
  const marked = useRef(null)

  const load = useCallback(async () => {
    setError('')
    try {
      setFile(await rest(fileUrl(dir, path)))
    } catch (e) {
      setError(String(e?.message || e))
    }
  }, [dir, path])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (file && line && marked.current) marked.current.scrollIntoView({ block: 'center' })
  }, [file, line])

  const save = async () => {
    try {
      await rest(fileUrl(dir, path), { method: 'PUT', body: { content: draft } })
      setDraft(null)
      await load()
      notifyRefresh()
      host.notify({ kind: 'success', message: `Saved ${path}` })
    } catch (e) {
      host.notify({ kind: 'error', message: `Could not save ${path}: ${e?.message || e}` })
    }
  }

  const editing = draft !== null
  const lines = (file?.content ?? '').split('\n')
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop()

  const toolbar = jsxs('div', {
    className: 'flex items-center gap-2 border-b border-(--ui-stroke-secondary) px-3 py-1.5 text-xs',
    children: [
      jsx('span', { className: 'font-mono text-(--ui-text-secondary)', children: path }),
      jsx('span', { className: 'text-(--ui-text-tertiary)', children: '· runbox workspace' }),
      jsx('span', { className: 'mr-auto' }),
      editing
        ? jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => setDraft(null), children: 'Cancel' })
        : jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => void load(), children: 'Reload' }),
      editing
        ? jsx(Button, { size: 'xs', onClick: () => void save(), children: 'Save' })
        : jsx(Button, { size: 'xs', variant: 'ghost', disabled: !file, onClick: () => setDraft(file?.content ?? ''), children: 'Edit' })
    ]
  })

  const body = error
    ? jsx(EmptyState, { title: 'Could not open this file', description: error })
    : editing
      ? jsx('textarea', {
          className: 'h-full w-full resize-none bg-transparent p-3 font-mono text-xs leading-5 outline-none',
          spellCheck: false,
          value: draft,
          onChange: (e) => setDraft(e.target.value)
        })
      : jsx('div', {
          className: 'grid font-mono text-xs leading-5',
          style: { gridTemplateColumns: 'auto 1fr' },
          children: lines.flatMap((text, i) => {
            const hit = line === i + 1
            const style = hit ? { background: tint(18) } : undefined
            return [
              jsx('span', { ref: hit ? marked : undefined, style, className: 'select-none pr-3 pl-3 text-right text-(--ui-text-quaternary)', children: i + 1 }, `n${i}`),
              jsx('span', { style, className: 'whitespace-pre pr-3', children: text || ' ' }, `l${i}`)
            ]
          })
        })

  return jsxs('div', {
    className: 'flex h-full flex-col overflow-hidden',
    children: [toolbar, jsx('div', { className: 'min-h-0 flex-1 overflow-auto py-2', children: body })]
  })
}

function openFile(dir, path, line, fallback) {
  if (typeof host.openWorkspace === 'function') {
    host.openWorkspace(`runbox-file-${dir}-${path}`, {
      title: path.split('/').pop(),
      minWidth: 320,
      render: () => jsx(FileView, { dir, path, line })
    })
  } else {
    fallback({ dir, path, line })
  }
}

// --- send a run's result to the chat --------------------------------------------------------

function runSummary(run) {
  const head = `runbox ${run.action}${run.command ? ` (\`${run.command}\`)` : ''}: ${run.ok ? 'passed' : 'failed'}`
  const issues = (run.issues || []).map(renderIssue)
  const detail = issues.length ? issues.join('\n') : (run.output || '').trim().split('\n').slice(-25).join('\n')
  return detail ? `${head}\n\`\`\`\n${detail}\n\`\`\`` : head
}

async function sendToChat(row, run) {
  const text = runSummary(run)
  const focusedStored = host.state.focusedStoredSessionId.get()
  const focused = host.state.focusedSessionId.get()
  if (row.session_id !== focusedStored && row.session_id !== focused) {
    host.openSession(row.session_id)
    await new Promise((resolve) => setTimeout(resolve, 700))
  }
  const ok = await host.composer.insertText(null, text, { mode: 'block' })
  if (ok) host.composer.focus(null)
  else host.notify({ kind: 'error', message: 'Could not reach the chat composer' })
}

// --- the tab ---------------------------------------------------------------------------------

function RunRow({ run, row, onOpenIssue }) {
  const [open, setOpen] = useState(false)
  const failed = run.ok === false
  const issues = run.issues || []
  return jsxs('div', {
    className: 'border-b border-(--ui-stroke-secondary) px-3 py-1',
    children: [
      jsxs('button', {
        type: 'button',
        className: 'flex w-full items-center gap-2 text-left text-xs',
        onClick: () => setOpen(!open),
        children: [
          jsx('span', { style: { color: failed ? FAIL_COLOR : OK_COLOR }, children: failed ? '✗' : '✓' }),
          jsx('span', { className: 'font-semibold text-(--ui-text-secondary)', children: run.action }),
          jsx('span', { className: 'min-w-0 flex-1 truncate font-mono text-(--ui-text-tertiary)', children: run.command || '' }),
          issues.length ? jsx('span', { className: 'text-(--ui-text-tertiary)', children: `${issues.length} issue${issues.length === 1 ? '' : 's'}` }) : null,
          jsx('span', { className: 'text-(--ui-text-quaternary)', children: `${run.by === 'desktop' ? 'you' : 'agent'} · ${fmtAgo(run.ts)}` })
        ]
      }),
      open
        ? jsxs('div', {
            className: 'pt-1 pb-1.5 pl-5',
            children: [
              ...issues.map((issue, i) =>
                jsx(
                  'button',
                  {
                    type: 'button',
                    className: 'block w-full truncate text-left font-mono text-xs text-(--ui-text-primary) hover:underline',
                    title: 'Open the file at this line',
                    onClick: () => issue.file && onOpenIssue(issue.file, issue.line),
                    children: renderIssue(issue)
                  },
                  `i${i}`
                )
              ),
              run.output
                ? jsx('pre', {
                    className: 'mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded p-2 font-mono text-[0.7rem] text-(--ui-text-secondary)',
                    style: { background: tint(6) },
                    children: run.output
                  })
                : null,
              jsxs('div', {
                className: 'flex items-center gap-1 pt-1',
                children: [
                  jsx('span', { className: 'mr-auto text-[0.7rem] text-(--ui-text-quaternary)', children: run.duration_ms != null ? `${run.duration_ms} ms${run.exit_code != null ? ` · exit ${run.exit_code}` : ''}` : '' }),
                  jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => void sendToChat(row, run), children: 'Send to chat' })
                ]
              })
            ]
          })
        : null
    ]
  })
}

function RunboxPane({ ctx }) {
  const focusedStored = useValue(host.state.focusedStoredSessionId)
  const focused = useValue(host.state.focusedSessionId)
  const [rows, setRows] = useState(null)
  const [titles, setTitles] = useState(new Map())
  const [picked, setPicked] = useState(null)
  const [files, setFiles] = useState(null)
  const [runs, setRuns] = useState([])
  const [command, setCommand] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [inline, setInline] = useState(null)
  const [confirm, setConfirm] = useState(null)
  const prefilledFor = useRef(null)

  const row =
    (rows || []).find((r) => r.dir === picked) ||
    (rows || []).find((r) => r.session_id === focusedStored || r.session_id === focused) ||
    null

  const refresh = useCallback(async () => {
    setError('')
    try {
      const [inv, names] = await Promise.all([rest('/sessions'), loadTitles()])
      setRows(inv.sessions || [])
      setTitles(names)
    } catch (e) {
      setError(String(e?.message || e))
      setRows([])
    }
  }, [])

  const dir = row?.dir
  const refreshBox = useCallback(async () => {
    if (!dir) {
      setFiles(null)
      setRuns([])
      return
    }
    try {
      const [f, r] = await Promise.all([rest(`/sessions/${encodeURIComponent(dir)}/files`), rest(`/sessions/${encodeURIComponent(dir)}/runs`)])
      setFiles(f)
      setRuns(r.runs || [])
      if (prefilledFor.current !== dir) {
        // Start from the chat's last run command.
        prefilledFor.current = dir
        const last = (r.runs || []).find((x) => x.action === 'run' && x.command)
        setCommand(last ? last.command : '')
      }
    } catch (e) {
      setError(String(e?.message || e))
    }
  }, [dir])

  useEffect(() => {
    const all = () => {
      void refresh()
      void refreshBox()
    }
    all()
    refreshListeners.add(all)
    const stopTimer = ctx.setInterval(all, 15000)
    return () => {
      refreshListeners.delete(all)
      stopTimer()
    }
  }, [refresh, refreshBox, ctx])

  const act = async (action, body = {}) => {
    if (!row) return
    setBusy(action)
    try {
      const res = await rest(`/sessions/${encodeURIComponent(row.dir)}/actions/${action}`, { method: 'POST', body })
      if (res?.success === false) host.notify({ kind: 'error', message: `runbox ${action}: ${res.error}` })
    } catch (e) {
      host.notify({ kind: 'error', message: `runbox ${action}: ${e?.message || e}` })
    } finally {
      setBusy('')
      await refresh()
      await refreshBox()
    }
  }

  const openIssue = (path, line) => openFile(row.dir, path, line, setInline)

  if (inline) {
    return jsxs('div', {
      className: 'flex h-full flex-col',
      children: [
        jsx(Button, { size: 'xs', variant: 'ghost', className: 'self-start m-1', onClick: () => setInline(null), children: '‹ Back' }),
        jsx('div', { className: 'min-h-0 flex-1', children: jsx(FileView, inline) })
      ]
    })
  }

  const chatLabel = (r) => titles.get(r.session_id) || (r.exists === false ? 'Deleted chat' : 'Untitled chat')
  const running = row?.container === 'running'

  const header = jsxs('div', {
    className: 'flex items-center gap-1 px-3 pt-2 pb-1',
    children: [
      jsx('div', { className: 'text-xs font-semibold tracking-wide text-(--ui-text-secondary)', children: 'RUNBOX' }),
      rows && rows.length
        ? jsx('select', {
            className: 'ml-1 min-w-0 flex-1 truncate rounded border border-(--ui-stroke-secondary) bg-transparent px-1 py-0.5 text-xs text-(--ui-text-primary)',
            value: row?.dir || '',
            onChange: (e) => setPicked(e.target.value || null),
            children: [
              row ? null : jsx('option', { value: '', children: 'Pick a chat…' }, ''),
              ...rows.map((r) => jsx('option', { value: r.dir, children: `${chatLabel(r)} · ${r.files} file${r.files === 1 ? '' : 's'}` }, r.dir))
            ]
          })
        : jsx('span', { className: 'flex-1' }),
      row
        ? jsx('span', {
            className: 'text-[0.65rem] text-(--ui-text-quaternary)',
            style: running ? { color: OK_COLOR } : undefined,
            title: running ? 'Container running' : 'Container stopped (starts on the next action)',
            children: running ? '● running' : '○ stopped'
          })
        : null,
      jsx(Button, { size: 'micro', variant: 'ghost', title: 'Refresh', onClick: () => notifyRefresh(), children: '↻' })
    ]
  })

  if (rows === null) return jsxs('div', { children: [header, jsx('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)', children: 'Loading…' })] })
  if (!row) {
    return jsxs('div', {
      className: 'flex h-full flex-col',
      children: [
        header,
        jsx(EmptyState, {
          title: error ? 'Could not load runbox' : rows.length ? 'This chat has no box yet' : 'No boxes yet',
          description: error || 'When the agent syncs code into runbox, the chat’s files and runs show here. Pick another chat above to see its box.'
        })
      ]
    })
  }

  const actions = jsxs('div', {
    className: 'flex items-center gap-1 px-3 pb-2',
    children: [
      jsx(Input, {
        className: 'h-7 min-w-0 flex-1 font-mono text-xs',
        placeholder: 'python main.py',
        value: command,
        disabled: !!busy,
        onChange: (e) => setCommand(e.target.value),
        onKeyDown: (e) => {
          if (e.key === 'Enter' && command.trim()) void act('run', { command })
        }
      }),
      jsx(Button, { size: 'xs', disabled: !!busy || !command.trim(), onClick: () => void act('run', { command }), children: busy === 'run' ? 'Running…' : 'Run' }),
      jsx(Button, { size: 'xs', variant: 'ghost', disabled: !!busy, onClick: () => void act('check'), children: busy === 'check' ? 'Checking…' : 'Check' }),
      jsx(Button, { size: 'xs', variant: 'ghost', disabled: !!busy, onClick: () => void act('test'), children: busy === 'test' ? 'Testing…' : 'Test' }),
      jsx(Button, { size: 'xs', variant: 'ghost', disabled: !!busy || !running, title: 'Remove the container (files are kept)', onClick: () => void act('stop'), children: 'Stop' })
    ]
  })

  const section = (label, extra) =>
    jsxs('div', {
      className: 'flex items-center px-3 pt-2 pb-0.5 text-[0.65rem] font-semibold tracking-wider text-(--ui-text-quaternary) uppercase',
      children: [jsx('span', { className: 'mr-auto', children: label }), extra]
    })

  const fileList = (files?.files || []).map((f) =>
    jsxs(
      'div',
      {
        className: 'flex items-center gap-1 px-3',
        children: [
          jsx('button', {
            type: 'button',
            className: 'min-w-0 flex-1 truncate text-left font-mono text-xs text-(--ui-text-secondary) hover:text-(--ui-text-primary) hover:underline',
            title: `Open ${f.path} · ${fmtBytes(f.bytes)}`,
            onClick: () => openFile(row.dir, f.path, null, setInline),
            children: f.path
          }),
          jsx(Button, {
            size: 'micro',
            variant: 'ghost',
            className: 'text-(--ui-text-quaternary)',
            title: `Delete ${f.path}`,
            onClick: () => setConfirm({ path: f.path }),
            children: '×'
          })
        ]
      },
      f.path
    )
  )

  const dialog = confirm
    ? jsx(ConfirmDialog, {
        open: true,
        destructive: true,
        onClose: () => setConfirm(null),
        title: `Delete ${confirm.path}?`,
        description: 'The file is removed from this chat’s runbox workspace.',
        confirmLabel: 'Delete',
        onConfirm: async () => {
          try {
            await rest(fileUrl(row.dir, confirm.path), { method: 'DELETE' })
          } catch (e) {
            host.notify({ kind: 'error', message: `runbox: ${e?.message || e}` })
          }
          setConfirm(null)
          notifyRefresh()
        }
      })
    : null

  return jsxs('div', {
    className: 'flex h-full flex-col overflow-hidden',
    children: [
      header,
      actions,
      jsxs('div', {
        className: 'min-h-0 flex-1 overflow-y-auto',
        children: [
          section(`Files${files ? ` · ${files.count}` : ''}`, null),
          fileList.length ? fileList : jsx('div', { className: 'px-3 text-xs text-(--ui-text-tertiary)', children: 'No files' }),
          files?.truncated ? jsx('div', { className: 'px-3 text-xs text-(--ui-text-quaternary)', children: '…more files not shown' }) : null,
          section('Runs', null),
          runs.length
            ? runs.map((run, i) => jsx(RunRow, { run, row, onOpenIssue: openIssue }, `${run.ts}-${i}`))
            : jsx('div', { className: 'px-3 text-xs text-(--ui-text-tertiary)', children: 'No runs yet' })
        ]
      }),
      dialog
    ]
  })
}

export default {
  id: 'runbox',
  name: 'runbox',
  register(ctx) {
    rest = ctx.rest
    // Refresh whenever the agent uses runbox or the backend reports a change.
    ctx.onEvent('tool.complete', (event) => {
      if (event?.payload?.name === 'runbox') notifyRefresh()
    })
    ctx.onEvent('plugin.runbox.runs.updated', () => notifyRefresh())
    ctx.register({
      id: 'runbox-terminal-tab',
      area: 'panes',
      title: 'Runbox',
      data: { placement: 'bottom', dock: { pane: 'terminal', pos: 'center' } },
      render: () => jsx(RunboxPane, { ctx })
    })
  }
}
