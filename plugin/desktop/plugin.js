// runbox Desktop half: a small VS Code-style editor that opens BESIDE the chat (where previews
// land). Top: the chat's workspace files as tabs, edited in place with line numbers, light Python
// colouring and problem markers. Toolbar: command + Run / Check / Test / Stop (the same backend
// actions as the agent's tool). Bottom panel: PROBLEMS, OUTPUT and RUNS, each sendable to chat.
// It follows the focused chat; a header picker shows another chat's box. It opens by itself the
// first time the agent uses runbox in a chat, and from the title bar or ⌘K "Open runbox".
import { Button, ConfirmDialog, EmptyState, Input, PALETTE_AREA, TITLEBAR_AREAS, host, icons, useValue } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

let rest = null
let pluginCtx = null
const refreshListeners = new Set()
const notifyRefresh = () => refreshListeners.forEach((fn) => fn())
const autoOpened = new Set()

const LINE_PX = 20
const OK_COLOR = '#3fb950'
const FAIL_COLOR = '#f85149'
const WARN_BG = 'color-mix(in srgb, #f85149 14%, transparent)'
const tint = (pct) => `color-mix(in srgb, var(--ui-accent) ${pct}%, transparent)`
const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'

const sessionPath = (dir) => `/sessions/${encodeURIComponent(dir)}`
const fileUrl = (dir, path) => `${sessionPath(dir)}/file?path=${encodeURIComponent(path)}`

// --- helpers -----------------------------------------------------------------------------------

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

// Problems = the latest check, the latest test and the latest failing run, de-duplicated.
function problemsOf(runs) {
  const seen = new Set()
  const out = []
  for (const action of ['check', 'test', 'run']) {
    const latest = runs.find((r) => r.action === action)
    for (const issue of latest?.issues || []) {
      const key = renderIssue(issue)
      if (!seen.has(key)) {
        seen.add(key)
        out.push({ ...issue, from: action })
      }
    }
  }
  return out
}

// --- light Python colouring ----------------------------------------------------------------

const PY_KEYWORDS = new Set(('False None True and as assert async await break class continue def del elif else except ' +
  'finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case').split(' '))
const PY_BUILTINS = new Set(('print len range str int float list dict set tuple bool open input enumerate zip map filter ' +
  'sorted sum min max abs isinstance type super self cls Exception ValueError TypeError KeyError IndexError').split(' '))
const COLORS = { kw: '#c678dd', str: '#98c379', com: 'var(--ui-text-quaternary)', num: '#d19a66', bi: '#61afef', fn: '#e5c07b', deco: '#56b6c2' }
const TOKEN = /(#.*$)|("""|''')|([rbuf]{0,2}"(?:[^"\\]|\\.)*"?|[rbuf]{0,2}'(?:[^'\\]|\\.)*'?)|(@[\w.]+)|(\b\d[\d_]*(?:\.\d+)?(?:e[+-]?\d+)?j?\b)|([A-Za-z_]\w*)/g

function highlightPython(text) {
  const lines = text.split('\n')
  let inTriple = null
  return lines.map((line, n) => {
    const parts = []
    let i = 0
    let prevWord = ''
    const push = (s, color) => parts.push(color ? jsx('span', { style: { color }, children: s }, parts.length) : s)
    while (i < line.length) {
      if (inTriple) {
        const end = line.indexOf(inTriple, i)
        if (end === -1) {
          push(line.slice(i), COLORS.str)
          i = line.length
        } else {
          push(line.slice(i, end + 3), COLORS.str)
          i = end + 3
          inTriple = null
        }
        continue
      }
      TOKEN.lastIndex = i
      const m = TOKEN.exec(line)
      if (!m) {
        push(line.slice(i))
        break
      }
      if (m.index > i) push(line.slice(i, m.index))
      const [tok, com, triple, str, deco, num, word] = m
      if (com) push(com, COLORS.com)
      else if (triple) {
        inTriple = triple
        push(triple, COLORS.str)
      } else if (str) push(str, COLORS.str)
      else if (deco) push(deco, COLORS.deco)
      else if (num) push(num, COLORS.num)
      else if (word) {
        if (PY_KEYWORDS.has(word)) push(word, COLORS.kw)
        else if (prevWord === 'def' || prevWord === 'class') push(word, COLORS.fn)
        else if (PY_BUILTINS.has(word)) push(word, COLORS.bi)
        else push(word)
        prevWord = word
      }
      i = m.index + tok.length
      if (!tok.length) i += 1
    }
    return jsx('div', { style: { height: LINE_PX }, children: parts.length ? parts : ' ' }, n)
  })
}

// --- send a result to the chat ----------------------------------------------------------------

async function sendToChat(row, text) {
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

function runText(run) {
  const head = `runbox ${run.action}${run.command ? ` (\`${run.command}\`)` : ''}: ${run.ok ? 'passed' : 'failed'}`
  const issues = (run.issues || []).map(renderIssue)
  const detail = issues.length ? issues.join('\n') : (run.output || '').trim().split('\n').slice(-25).join('\n')
  return detail ? `${head}\n\`\`\`\n${detail}\n\`\`\`` : head
}

// --- the code editor --------------------------------------------------------------------------

function CodeEditor({ text, onChange, onSave, isPython, problems, jump }) {
  const scroller = useRef(null)
  const area = useRef(null)
  const lines = text.split('\n')
  const longest = lines.reduce((n, l) => Math.max(n, l.length), 0)
  const byLine = useMemo(() => {
    const map = new Map()
    for (const p of problems) if (p.line) map.set(p.line, [...(map.get(p.line) || []), p])
    return map
  }, [problems])
  const colored = useMemo(() => (isPython ? highlightPython(text) : null), [text, isPython])

  useEffect(() => {
    if (!jump?.line || !scroller.current) return
    scroller.current.scrollTop = Math.max(0, (jump.line - 4) * LINE_PX)
    const ta = area.current
    if (ta) {
      const offset = lines.slice(0, jump.line - 1).reduce((n, l) => n + l.length + 1, 0)
      ta.focus({ preventScroll: true })
      ta.setSelectionRange(offset, offset + (lines[jump.line - 1] || '').length)
    }
  }, [jump]) // eslint-disable-line react-hooks/exhaustive-deps

  const onKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 's') {
      e.preventDefault()
      onSave()
    } else if (e.key === 'Tab' && !e.shiftKey) {
      e.preventDefault()
      const ta = e.currentTarget
      const { selectionStart: a, selectionEnd: b } = ta
      onChange(text.slice(0, a) + '    ' + text.slice(b))
      requestAnimationFrame(() => ta.setSelectionRange(a + 4, a + 4))
    }
  }

  const height = lines.length * LINE_PX + 16
  const shared = { fontFamily: MONO, fontSize: 12.5, lineHeight: `${LINE_PX}px`, padding: '8px 12px', whiteSpace: 'pre', tabSize: 4 }

  return jsx('div', {
    ref: scroller,
    className: 'min-h-0 flex-1 overflow-auto',
    children: jsxs('div', {
      style: { display: 'grid', gridTemplateColumns: 'auto 1fr', minHeight: '100%' },
      children: [
        jsx('div', {
          className: 'select-none text-right text-(--ui-text-quaternary)',
          style: { ...shared, padding: '8px 10px 8px 12px', position: 'sticky', left: 0, zIndex: 1, background: 'var(--ui-editor-background, inherit)' },
          children: lines.map((_, i) => {
            const issues = byLine.get(i + 1)
            return jsx(
              'div',
              {
                style: { height: LINE_PX, color: issues ? FAIL_COLOR : undefined },
                title: issues ? issues.map(renderIssue).join('\n') : undefined,
                children: issues ? `● ${i + 1}` : i + 1
              },
              i
            )
          })
        }),
        jsxs('div', {
          style: { position: 'relative', height, minWidth: `calc(${longest + 4}ch + 24px)` },
          children: [
            jsx('div', {
              'aria-hidden': true,
              style: { position: 'absolute', inset: 0, pointerEvents: 'none' },
              children: lines.map((_, i) =>
                byLine.get(i + 1) || jump?.line === i + 1
                  ? jsx('div', { style: { position: 'absolute', left: 0, right: 0, top: 8 + i * LINE_PX, height: LINE_PX, background: byLine.get(i + 1) ? WARN_BG : tint(14) } }, i)
                  : null
              )
            }),
            jsx('div', {
              'aria-hidden': true,
              style: { ...shared, position: 'absolute', inset: 0, pointerEvents: 'none', color: 'var(--ui-text-primary)' },
              children: colored || lines.map((l, i) => jsx('div', { style: { height: LINE_PX }, children: l || ' ' }, i))
            }),
            jsx('textarea', {
              ref: area,
              value: text,
              spellCheck: false,
              wrap: 'off',
              onChange: (e) => onChange(e.target.value),
              onKeyDown,
              style: {
                ...shared,
                position: 'absolute',
                inset: 0,
                width: '100%',
                height: '100%',
                resize: 'none',
                overflow: 'hidden',
                background: 'transparent',
                color: 'transparent',
                caretColor: 'var(--ui-text-primary)',
                border: 0,
                outline: 'none'
              }
            })
          ]
        })
      ]
    })
  })
}

// --- bottom panel ------------------------------------------------------------------------------

function PanelTabs({ tab, setTab, problems, children }) {
  const item = (id, label) =>
    jsx(
      'button',
      {
        type: 'button',
        onClick: () => setTab(id),
        className: `px-2 py-1 text-[0.68rem] font-semibold tracking-wider uppercase ${tab === id ? 'text-(--ui-text-primary)' : 'text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)'}`,
        style: tab === id ? { boxShadow: 'inset 0 -1px 0 var(--ui-accent)' } : undefined,
        children: label
      },
      id
    )
  return jsxs('div', {
    className: 'flex items-center gap-1 border-b border-(--ui-stroke-secondary) px-2',
    children: [item('problems', `Problems${problems.length ? ` ${problems.length}` : ''}`), item('output', 'Output'), item('runs', 'Runs'), jsx('span', { className: 'flex-1' }), children]
  })
}

function RunList({ runs, row }) {
  const [open, setOpen] = useState(null)
  if (!runs.length) return jsx('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)', children: 'No runs yet' })
  return runs.map((run, i) => {
    const key = `${run.ts}-${i}`
    const failed = run.ok === false
    return jsxs(
      'div',
      {
        className: 'border-b border-(--ui-stroke-secondary) px-3 py-1',
        children: [
          jsxs('button', {
            type: 'button',
            className: 'flex w-full items-center gap-2 text-left text-xs',
            onClick: () => setOpen(open === key ? null : key),
            children: [
              jsx('span', { style: { color: failed ? FAIL_COLOR : OK_COLOR }, children: failed ? '✗' : '✓' }),
              jsx('span', { className: 'font-semibold text-(--ui-text-secondary)', children: run.action }),
              jsx('span', { className: 'min-w-0 flex-1 truncate font-mono text-(--ui-text-tertiary)', children: run.command || '' }),
              jsx('span', { className: 'text-(--ui-text-quaternary)', children: `${run.by === 'desktop' ? 'you' : 'agent'} · ${fmtAgo(run.ts)}` })
            ]
          }),
          open === key
            ? jsxs('div', {
                className: 'pt-1 pb-1.5 pl-5',
                children: [
                  ...(run.issues || []).map((issue, j) => jsx('div', { className: 'font-mono text-xs text-(--ui-text-primary)', children: renderIssue(issue) }, j)),
                  run.output ? jsx('pre', { className: 'mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded p-2 font-mono text-[0.7rem] text-(--ui-text-secondary)', style: { background: tint(6) }, children: run.output }) : null,
                  jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => void sendToChat(row, runText(run)), children: 'Send to chat' })
                ]
              })
            : null
        ]
      },
      key
    )
  })
}

// --- the editor tile ---------------------------------------------------------------------------

function RunboxEditor() {
  const focusedStored = useValue(host.state.focusedStoredSessionId)
  const focused = useValue(host.state.focusedSessionId)
  const [rows, setRows] = useState(null)
  const [titles, setTitles] = useState(new Map())
  const [picked, setPicked] = useState(null)
  const [files, setFiles] = useState([])
  const [active, setActive] = useState(null)
  const [buffers, setBuffers] = useState({}) // path -> { saved, draft }
  const [runs, setRuns] = useState([])
  const [command, setCommand] = useState('')
  const [commandTouched, setCommandTouched] = useState(false)
  const [busy, setBusy] = useState('')
  const [tab, setTab] = useState('problems')
  const [panelPx, setPanelPx] = useState(220)
  const [jump, setJump] = useState(null)
  const [error, setError] = useState('')
  const [newName, setNewName] = useState(null)
  const [confirm, setConfirm] = useState(null)

  const row =
    (rows || []).find((r) => r.dir === picked) || (rows || []).find((r) => r.session_id === focusedStored || r.session_id === focused) || null
  const dir = row?.dir

  const loadFile = useCallback(
    async (path, force = false) => {
      if (!dir) return
      try {
        const f = await rest(fileUrl(dir, path))
        setBuffers((b) => {
          const cur = b[path]
          if (cur && cur.draft !== cur.saved && !force) return { ...b, [path]: { ...cur, saved: f.content } }
          return { ...b, [path]: { saved: f.content, draft: f.content } }
        })
      } catch (e) {
        setBuffers((b) => ({ ...b, [path]: { saved: '', draft: '', error: String(e?.message || e) } }))
      }
    },
    [dir]
  )

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

  const refreshBox = useCallback(async () => {
    if (!dir) return
    try {
      const [f, r] = await Promise.all([rest(`${sessionPath(dir)}/files`), rest(`${sessionPath(dir)}/runs`)])
      const list = (f.files || []).map((x) => x.path)
      setFiles(list)
      setRuns(r.runs || [])
      setActive((cur) => (cur && list.includes(cur) ? cur : list.find((p) => p.endsWith('.py') && !p.split('/').pop().startsWith('test_')) || list[0] || null))
    } catch (e) {
      setError(String(e?.message || e))
    }
  }, [dir])

  // Switching chats starts clean.
  useEffect(() => {
    setBuffers({})
    setActive(null)
    setCommandTouched(false)
    setJump(null)
  }, [dir])

  useEffect(() => {
    const all = async () => {
      await refresh()
      await refreshBox()
    }
    void all()
    refreshListeners.add(all)
    const stop = pluginCtx.setInterval(() => void all(), 15000)
    return () => {
      refreshListeners.delete(all)
      stop()
    }
  }, [refresh, refreshBox])

  // Keep the open file in step with the box (the agent may have changed it).
  useEffect(() => {
    if (active) void loadFile(active)
  }, [active, runs, loadFile])

  useEffect(() => {
    if (!commandTouched) setCommand(active && active.endsWith('.py') ? `python ${active}` : '')
  }, [active, commandTouched])

  const buf = active ? buffers[active] : null
  const dirty = (path) => buffers[path] && buffers[path].draft !== buffers[path].saved
  const problems = problemsOf(runs)
  const running = row?.container === 'running'

  const save = async (path) => {
    const b = buffers[path]
    if (!b || b.draft === b.saved) return true
    try {
      await rest(fileUrl(dir, path), { method: 'PUT', body: { content: b.draft } })
      setBuffers((x) => ({ ...x, [path]: { saved: b.draft, draft: b.draft } }))
      return true
    } catch (e) {
      host.notify({ kind: 'error', message: `Could not save ${path}: ${e?.message || e}` })
      return false
    }
  }

  const act = async (action, body = {}) => {
    if (!row) return
    for (const path of Object.keys(buffers)) if (dirty(path) && !(await save(path))) return
    setBusy(action)
    setTab(action === 'run' ? 'output' : action === 'stop' ? tab : 'problems')
    try {
      const res = await rest(`${sessionPath(dir)}/actions/${action}`, { method: 'POST', body })
      if (res?.success === false) host.notify({ kind: 'error', message: `runbox ${action}: ${res.error}` })
    } catch (e) {
      host.notify({ kind: 'error', message: `runbox ${action}: ${e?.message || e}` })
    } finally {
      setBusy('')
      await refresh()
      await refreshBox()
    }
  }

  const goTo = (issue) => {
    if (!issue.file || !files.includes(issue.file)) return
    setActive(issue.file)
    setJump({ line: issue.line, at: Date.now() })
  }

  const createFile = async () => {
    const name = (newName || '').trim()
    setNewName(null)
    if (!name || !dir) return
    try {
      await rest(fileUrl(dir, name), { method: 'PUT', body: { content: '' } })
      await refreshBox()
      setActive(name)
    } catch (e) {
      host.notify({ kind: 'error', message: `Could not create ${name}: ${e?.message || e}` })
    }
  }

  const startDrag = (e) => {
    const startY = e.clientY
    const start = panelPx
    const move = (ev) => setPanelPx(Math.max(90, Math.min(window.innerHeight - 200, start - (ev.clientY - startY))))
    const up = () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  const chatLabel = (r) => titles.get(r.session_id) || (r.exists === false ? 'Deleted chat' : 'Untitled chat')

  // ----- header: chat picker + status
  const header = jsxs('div', {
    className: 'flex items-center gap-2 border-b border-(--ui-stroke-secondary) px-3 py-1.5',
    children: [
      jsx('span', { className: 'text-xs font-semibold tracking-wide text-(--ui-text-secondary)', children: 'RUNBOX' }),
      rows && rows.length
        ? jsx('select', {
            className: 'min-w-0 max-w-64 truncate rounded border border-(--ui-stroke-secondary) bg-transparent px-1 py-0.5 text-xs text-(--ui-text-primary)',
            value: row?.dir || '',
            onChange: (e) => setPicked(e.target.value || null),
            children: [
              row ? null : jsx('option', { value: '', children: 'Pick a chat…' }, ''),
              ...rows.map((r) => jsx('option', { value: r.dir, children: chatLabel(r) }, r.dir))
            ]
          })
        : null,
      row
        ? jsx('span', { className: 'text-[0.68rem] text-(--ui-text-quaternary)', style: running ? { color: OK_COLOR } : undefined, title: running ? 'Container running' : 'Starts on the next action', children: running ? '● running' : '○ stopped' })
        : null,
      jsx('span', { className: 'flex-1' }),
      jsx(Button, { size: 'micro', variant: 'ghost', title: 'Refresh', onClick: () => notifyRefresh(), children: '↻' })
    ]
  })

  if (rows === null) return jsxs('div', { className: 'flex h-full flex-col', children: [header, jsx('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)', children: 'Loading…' })] })
  if (!row) {
    return jsxs('div', {
      className: 'flex h-full flex-col',
      children: [
        header,
        jsx(EmptyState, {
          title: error ? 'Could not load runbox' : 'This chat has no box yet',
          description: error || 'The agent creates one when it first uses runbox in this chat. Pick another chat above to see its box.'
        })
      ]
    })
  }

  // ----- file tabs + toolbar
  const fileTab = (path) =>
    jsxs(
      'div',
      {
        className: `group flex items-center gap-1 border-r border-(--ui-stroke-secondary) px-2 py-1 text-xs ${path === active ? 'text-(--ui-text-primary)' : 'text-(--ui-text-tertiary)'}`,
        style: path === active ? { boxShadow: 'inset 0 -1px 0 var(--ui-accent)' } : undefined,
        children: [
          jsx('button', { type: 'button', className: 'font-mono', onClick: () => setActive(path), children: path }),
          dirty(path) ? jsx('span', { title: 'Unsaved', children: '●' }) : null,
          jsx('button', { type: 'button', title: `Delete ${path}`, className: 'opacity-0 group-hover:opacity-60', onClick: () => setConfirm({ path }), children: '×' })
        ]
      },
      path
    )

  const tabsRow = jsxs('div', {
    className: 'flex items-center overflow-x-auto border-b border-(--ui-stroke-secondary)',
    children: [
      files.length > 8
        ? jsx('select', {
            className: 'm-1 rounded border border-(--ui-stroke-secondary) bg-transparent px-1 py-0.5 font-mono text-xs',
            value: active || '',
            onChange: (e) => setActive(e.target.value),
            children: files.map((p) => jsx('option', { value: p, children: `${p}${dirty(p) ? ' ●' : ''}` }, p))
          })
        : files.map(fileTab),
      newName !== null
        ? jsx(Input, {
            autoFocus: true,
            className: 'm-1 h-6 w-36 font-mono text-xs',
            placeholder: 'name.py',
            value: newName,
            onChange: (e) => setNewName(e.target.value),
            onBlur: () => void createFile(),
            onKeyDown: (e) => {
              if (e.key === 'Enter') void createFile()
              if (e.key === 'Escape') setNewName(null)
            }
          })
        : jsx(Button, { size: 'micro', variant: 'ghost', title: 'New file', onClick: () => setNewName(''), children: '+' })
    ]
  })

  const toolbar = jsxs('div', {
    className: 'flex items-center gap-1 border-b border-(--ui-stroke-secondary) px-2 py-1',
    children: [
      jsx(Input, {
        className: 'h-7 min-w-0 flex-1 font-mono text-xs',
        placeholder: 'python main.py',
        value: command,
        disabled: !!busy,
        onChange: (e) => {
          setCommand(e.target.value)
          setCommandTouched(true)
        },
        onKeyDown: (e) => {
          if (e.key === 'Enter' && command.trim()) void act('run', { command })
        }
      }),
      jsx(Button, { size: 'xs', disabled: !!busy || !command.trim(), onClick: () => void act('run', { command }), children: busy === 'run' ? 'Running…' : '▶ Run' }),
      jsx(Button, { size: 'xs', variant: 'ghost', disabled: !!busy, onClick: () => void act('check'), children: busy === 'check' ? 'Checking…' : 'Check' }),
      jsx(Button, { size: 'xs', variant: 'ghost', disabled: !!busy, onClick: () => void act('test'), children: busy === 'test' ? 'Testing…' : 'Test' }),
      jsx(Button, { size: 'xs', variant: 'ghost', disabled: !!busy || !running, title: 'Stop the container (files are kept)', onClick: () => void act('stop'), children: '■ Stop' })
    ]
  })

  const editor = !active
    ? jsx(EmptyState, { title: 'No files yet', description: 'Files the agent syncs into runbox show here. Use + to start one.' })
    : !buf
      ? jsx('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)', children: 'Loading…' })
      : buf.error
        ? jsx(EmptyState, { title: `Could not open ${active}`, description: buf.error })
        : jsx(CodeEditor, {
            text: buf.draft,
            isPython: active.endsWith('.py'),
            problems: problems.filter((p) => p.file === active),
            jump,
            onChange: (draft) => setBuffers((b) => ({ ...b, [active]: { ...b[active], draft } })),
            onSave: () => void save(active)
          })

  // ----- bottom panel
  const lastRun = runs.find((r) => r.action === 'run' || r.action === 'test')
  const sendButton =
    tab === 'problems' && problems.length
      ? jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => void sendToChat(row, `runbox problems:\n\`\`\`\n${problems.map(renderIssue).join('\n')}\n\`\`\``), children: 'Send to chat' })
      : tab === 'output' && lastRun
        ? jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => void sendToChat(row, runText(lastRun)), children: 'Send to chat' })
        : null

  const panelBody =
    tab === 'problems'
      ? problems.length
        ? problems.map((p, i) =>
            jsxs(
              'button',
              {
                type: 'button',
                className: 'flex w-full items-baseline gap-2 px-3 py-0.5 text-left font-mono text-xs hover:bg-(--ui-stroke-secondary)',
                title: p.file && files.includes(p.file) ? 'Go to this line' : undefined,
                onClick: () => goTo(p),
                children: [
                  jsx('span', { style: { color: FAIL_COLOR }, children: '✗' }),
                  jsx('span', { className: 'text-(--ui-text-secondary)', children: `${p.file || '?'}${p.line ? `:${p.line}` : ''}${p.col ? `:${p.col}` : ''}` }),
                  jsx('span', { className: 'text-(--ui-text-tertiary)', children: p.code }),
                  jsx('span', { className: 'min-w-0 flex-1 text-(--ui-text-primary)', children: p.message }),
                  jsx('span', { className: 'text-(--ui-text-quaternary)', children: p.from })
                ]
              },
              i
            )
          )
        : jsx('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)', children: runs.some((r) => r.action === 'check' || r.action === 'test') ? 'No problems found.' : 'Run Check or Test to look for problems.' })
      : tab === 'output'
        ? lastRun
          ? jsxs('div', {
              className: 'p-2 font-mono text-xs',
              children: [
                jsx('div', { className: 'text-(--ui-text-tertiary)', children: `$ ${lastRun.command || lastRun.action}` }),
                jsx('pre', { className: 'whitespace-pre-wrap text-(--ui-text-primary)', children: lastRun.output || '(no output)' }),
                jsx('div', {
                  style: { color: lastRun.ok ? OK_COLOR : FAIL_COLOR },
                  children: `${lastRun.ok ? 'done' : 'failed'}${lastRun.exit_code != null ? ` · exit ${lastRun.exit_code}` : ''}${lastRun.duration_ms != null ? ` · ${lastRun.duration_ms} ms` : ''} · ${lastRun.by === 'desktop' ? 'you' : 'agent'}, ${fmtAgo(lastRun.ts)}`
                })
              ]
            })
          : jsx('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)', children: 'Press ▶ Run to see output here.' })
        : jsx(RunList, { runs, row })

  const panel = jsxs('div', {
    className: 'flex flex-col border-t border-(--ui-stroke-secondary)',
    style: { height: panelPx },
    children: [
      jsx('div', { onMouseDown: startDrag, style: { height: 4, cursor: 'row-resize', marginTop: -2 } }),
      jsx(PanelTabs, { tab, setTab, problems, children: sendButton }),
      jsx('div', { className: 'min-h-0 flex-1 overflow-auto', children: panelBody })
    ]
  })

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
            await rest(fileUrl(dir, confirm.path), { method: 'DELETE' })
          } catch (e) {
            host.notify({ kind: 'error', message: `runbox: ${e?.message || e}` })
          }
          setBuffers((b) => {
            const { [confirm.path]: _gone, ...rest_ } = b
            return rest_
          })
          setConfirm(null)
          notifyRefresh()
        }
      })
    : null

  return jsxs('div', {
    className: 'flex h-full flex-col overflow-hidden',
    children: [header, tabsRow, toolbar, jsx('div', { className: 'flex min-h-0 flex-1 flex-col', children: editor }), panel, dialog]
  })
}

// --- registration ----------------------------------------------------------------------------

function openEditor() {
  if (typeof host.openWorkspace !== 'function') {
    host.notify({ kind: 'error', message: 'runbox needs a newer Hermes Desktop (openWorkspace)' })
    return
  }
  host.openWorkspace('runbox', {
    title: 'Runbox',
    minWidth: '26rem',
    dock: { pane: 'workspace', pos: 'right' },
    render: () => jsx(RunboxEditor, {})
  })
}

export default {
  id: 'runbox',
  name: 'runbox',
  register(ctx) {
    rest = ctx.rest
    pluginCtx = ctx
    // Open beside the chat the first time the agent uses runbox in it; refresh on every use.
    ctx.onEvent('tool.complete', (event) => {
      if (event?.payload?.name !== 'runbox') return
      const sid = event?.session_id || host.state.focusedSessionId.get() || ''
      if (!autoOpened.has(sid)) {
        autoOpened.add(sid)
        openEditor()
      }
      notifyRefresh()
    })
    ctx.onEvent('plugin.runbox.runs.updated', () => notifyRefresh())
    ctx.registerMany([
      {
        id: 'runbox-titlebar',
        area: TITLEBAR_AREAS.right,
        data: { id: 'runbox-open', label: 'Runbox', icon: jsx(icons.Box, { size: 15 }), onSelect: () => openEditor() }
      },
      {
        id: 'runbox-palette',
        area: PALETTE_AREA,
        data: { id: 'runbox.open', label: 'Open runbox', keywords: ['runbox', 'run', 'python', 'test'], run: () => openEditor() }
      }
    ])
  }
}
