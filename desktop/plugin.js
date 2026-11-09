// Guild: the team skill library page for Hermes Desktop.
// Talks to the Python half through plugin RPC (plugin.guild.*), over the gateway's own socket.
import {
  Badge,
  Button,
  EmptyState,
  LogView,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  PALETTE_AREA,
  Textarea,
  host,
  relativeTime,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { createElement as h, Fragment, useState } from 'react'

const KEY = ['plugin:guild', 'overview']
const call = async (method, params = {}) => {
  const res = await host.request(`plugin.guild.${method}`, params, 60000)
  if (!res || res.ok === false) throw new Error((res && res.error) || 'Guild request failed')
  return res
}
const ago = iso => {
  const t = Date.parse(iso || '')
  return Number.isFinite(t) ? relativeTime(t) : ''
}
const notifyErr = e => host.notify({ kind: 'error', message: String((e && e.message) || e) })

function Section({ title, count, children }) {
  return h('section', { className: 'flex flex-col gap-2' },
    h('div', { className: 'flex items-baseline gap-2 border-b border-(--ui-stroke-tertiary) pb-1.5' },
      h('h2', { className: 'text-[0.8125rem] font-medium text-(--ui-text-primary)' }, title),
      count === undefined ? null : h('span', { className: 'text-xs text-(--ui-text-quaternary)' }, String(count))),
    children)
}

function Diff({ text }) {
  const lines = (text || '').split('\n')
  return h(LogView, { className: 'max-h-80' }, lines.map((line, i) => {
    let cls = ''
    if (line.startsWith('+') && !line.startsWith('+++')) cls = 'text-emerald-600 dark:text-emerald-300'
    else if (line.startsWith('-') && !line.startsWith('---')) cls = 'text-destructive'
    else if (line.startsWith('@@')) cls = 'text-(--ui-text-quaternary)'
    return h('div', { key: i, className: cls }, line || ' ')
  }))
}

function ProposalRow({ p }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [declining, setDeclining] = useState(false)
  const [reason, setReason] = useState('')
  const detail = useQuery({ queryKey: ['plugin:guild', 'show', p.id], queryFn: () => call('show', { id: p.id }), enabled: open })
  const done = msg => { host.notify({ kind: 'success', message: msg }); qc.invalidateQueries({ queryKey: KEY }) }
  const approve = useMutation({
    mutationFn: () => call('approve', { id: p.id }),
    onSuccess: r => done(`Approved ${r.skill} v${r.version}. Teammates get it on their next sync.`),
    onError: notifyErr
  })
  const reject = useMutation({
    mutationFn: () => call('reject', { id: p.id, reason }),
    onSuccess: r => done(`Declined ${r.skill} from ${r.author}.`),
    onError: notifyErr
  })
  const busy = approve.isPending || reject.isPending
  const d = detail.data

  return h('div', { className: 'rounded-lg border border-(--ui-stroke-tertiary)' },
    h('button', {
      type: 'button',
      className: 'flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-(--chrome-action-hover)',
      onClick: () => setOpen(!open)
    },
      h('span', { className: 'text-sm font-medium text-(--ui-text-primary)' }, p.skill),
      h(Badge, { variant: p.kind === 'update' ? 'warn' : 'default' }, p.kind === 'update' ? 'change' : 'new'),
      h('span', { className: 'min-w-0 flex-1 truncate text-xs text-(--ui-text-tertiary)' },
        `from ${p.member}${p.note ? ' · ' + p.note : ''}`),
      h('span', { className: 'shrink-0 text-xs text-(--ui-text-quaternary)' }, ago(p.proposed_at))),
    open ? h('div', { className: 'flex flex-col gap-3 border-t border-(--ui-stroke-tertiary) px-3 py-3' },
      detail.isLoading ? h('div', { className: 'text-xs text-(--ui-text-tertiary)' }, 'Loading diff…') : null,
      detail.error ? h('div', { className: 'text-xs text-destructive' }, String(detail.error.message || detail.error)) : null,
      d ? h(Fragment, null,
        h('div', { className: 'flex flex-wrap gap-x-4 gap-y-1 text-xs text-(--ui-text-tertiary)' },
          h('span', null, `Removed before sending: ${(d.meta && d.meta.redactions) || 0}`),
          d.similar && d.similar.length
            ? h('span', null, 'Similar team skills: ' + d.similar.map(s => `${s.skill} (${Math.round(s.overlap * 100)}%)`).join(', '))
            : null,
          p.evidence && p.evidence.uses_by_author != null ? h('span', null, `Used ${p.evidence.uses_by_author}× by ${p.member}`) : null),
        h(Diff, { text: d.diff })) : null,
      declining ? h(Textarea, {
        value: reason,
        placeholder: 'Reason (optional), shown in the guild log',
        onChange: e => setReason(e.target.value),
        className: 'min-h-16 text-sm'
      }) : null,
      h('div', { className: 'flex gap-2' },
        declining
          ? h(Fragment, null,
            h(Button, { size: 'sm', variant: 'destructive', disabled: busy, onClick: () => reject.mutate() }, 'Decline'),
            h(Button, { size: 'sm', variant: 'ghost', disabled: busy, onClick: () => setDeclining(false) }, 'Cancel'))
          : h(Fragment, null,
            h(Button, { size: 'sm', disabled: busy, onClick: () => approve.mutate() }, approve.isPending ? 'Approving…' : 'Approve'),
            h(Button, { size: 'sm', variant: 'outline', disabled: busy, onClick: () => setDeclining(true) }, 'Decline')))) : null)
}

function SkillRow({ s }) {
  return h('div', { className: 'flex items-center gap-3 px-1 py-1.5 text-sm' },
    h('span', { className: 'font-medium text-(--ui-text-primary)' }, s.skill),
    h(Badge, { variant: 'muted' }, `v${s.version}`),
    h('span', { className: 'min-w-0 flex-1 truncate text-xs text-(--ui-text-tertiary)' },
      `by ${s.author} · approved by ${s.approved_by}`),
    s.uses_here ? h('span', { className: 'text-xs text-(--ui-text-quaternary)' }, `used ${s.uses_here}× here`) : null)
}

function CandidateRow({ skill }) {
  const qc = useQueryClient()
  const propose = useMutation({
    mutationFn: () => call('propose', { skill }),
    onSuccess: r => {
      host.notify({ kind: 'success', message: `Proposed ${skill}. Removed before sending: ${r.scrubbed}.` })
      qc.invalidateQueries({ queryKey: KEY })
    },
    onError: notifyErr
  })
  return h('div', { className: 'flex items-center gap-3 px-1 py-1.5 text-sm' },
    h('span', { className: 'min-w-0 flex-1 font-medium text-(--ui-text-primary)' }, skill),
    h(Button, { size: 'xs', variant: 'secondary', disabled: propose.isPending, onClick: () => propose.mutate() },
      propose.isPending ? 'Proposing…' : 'Propose to team'))
}

function GuildPage() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: KEY, queryFn: () => call('overview'), refetchInterval: 15000 })
  const sync = useMutation({
    mutationFn: () => call('sync'),
    onSuccess: r => {
      const bits = []
      if (r.added && r.added.length) bits.push(`new: ${r.added.join(', ')}`)
      if (r.updated && r.updated.length) bits.push(`updated: ${r.updated.join(', ')}`)
      host.notify({ kind: 'success', message: bits.length ? `Synced. ${bits.join('; ')}` : 'Synced. Nothing new.' })
      qc.invalidateQueries({ queryKey: KEY })
    },
    onError: notifyErr
  })

  const body = (() => {
    if (q.isLoading) return h('div', { className: 'text-sm text-(--ui-text-tertiary)' }, 'Loading…')
    if (q.error) return h(EmptyState, { title: 'Guild is not available', description: String(q.error.message || q.error) })
    const st = q.data
    if (!st.configured) {
      return h(EmptyState, {
        title: 'Not in a guild yet',
        description: 'In a terminal, start one with "hermes guild init <git remote> --name <team>" or join one with "hermes guild join <git remote>".'
      })
    }
    const props = st.proposals || []
    const skills = st.skills || []
    const cands = st.candidates || []
    return h(Fragment, null,
      h('header', { className: 'flex items-start gap-4' },
        h('div', { className: 'min-w-0 flex-1' },
          h('h1', { className: 'text-lg font-semibold text-(--ui-text-primary)' }, st.name),
          h('p', { className: 'mt-0.5 truncate text-xs text-(--ui-text-tertiary)' },
            `You are ${st.member} · ${st.remote} · synced ${ago(st.last_sync) || 'never'}`)),
        h(Button, { size: 'sm', variant: 'secondary', disabled: sync.isPending, onClick: () => sync.mutate() },
          sync.isPending ? 'Syncing…' : 'Sync')),
      h(Section, { title: 'Waiting for review', count: props.length },
        props.length
          ? props.map(p => h(ProposalRow, { key: p.id, p }))
          : h('p', { className: 'px-1 text-xs text-(--ui-text-quaternary)' }, 'Nothing to review.')),
      h(Section, { title: 'Team skills', count: skills.length },
        skills.length
          ? skills.map(s => h(SkillRow, { key: s.skill, s }))
          : h('p', { className: 'px-1 text-xs text-(--ui-text-quaternary)' }, 'No team skills yet.')),
      cands.length
        ? h(Section, { title: 'Learned here, not shared yet', count: cands.length },
          cands.map(c => h(CandidateRow, { key: c, skill: c })))
        : null,
      (st.shadowed || []).length
        ? h('p', { className: 'text-xs text-(--ui-text-tertiary)' },
          'Your local copy hides the team version of: ' + st.shadowed.map(r => r.skill).join(', '))
        : null)
  })()

  return h('div', { className: 'h-full overflow-auto' },
    h('div', { className: 'mx-auto flex max-w-3xl flex-col gap-6 px-6 pb-10 pt-6' }, body))
}

export default {
  id: 'guild',
  name: 'Guild',
  register(ctx) {
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/guild' }, render: () => h(GuildPage) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/guild', label: 'Guild', codicon: 'organization' } },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'guild.open',
          label: 'Guild: open team skills',
          keywords: ['guild', 'skills', 'team', 'review'],
          run: () => host.navigate('/guild')
        }
      }
    ])
  }
}
