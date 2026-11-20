// One invoice, laid out like the PDF, with payment details. Admins also get the actions:
// mark as paid, send a reminder, void.
import { h, field, input, errorArea, onSubmit, actionButton, confirmButton, toast } from '../dom.js';
import { get, post, download } from '../api.js';
import { fmtDate, money, isoDate, today } from '../format.js';

export const INVOICE_STATUS = {
  scheduled: (i) => [`Scheduled for ${fmtDate(i.issue_date)}`, ''],
  due: (i) => [`Due ${fmtDate(i.due_date)}`, ''],
  overdue: (i) => [`Overdue: was due ${fmtDate(i.due_date)}`, 'bad'],
  paid: (i) => [i.paid_on ? `Paid ${fmtDate(i.paid_on)}` : 'Paid', 'ok'],
  void: () => ['Void', 'bad'],
};

export function statusBadge(invoice) {
  const [label, tone] = (INVOICE_STATUS[invoice.display_status] || (() => [invoice.display_status, '']))(invoice);
  return h('span', { class: `badge ${tone}` }, label);
}

function facts(pairs) {
  return h('dl', { class: 'facts' }, pairs.filter(([, v]) => v).map(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]));
}

export function payBox(payment, reference) {
  return h('section', { class: 'pay-box', 'aria-labelledby': 'pay-title' },
    h('h2', { id: 'pay-title' }, 'How to pay: bank transfer'),
    facts([['Account name', payment.account_name], ['BSB', payment.bsb], ['Account number', payment.account_number],
      ['Reference', reference || 'The invoice number'], ['PayID', payment.payid]]),
    h('p', { class: 'small' }, 'Our bank details never change by email. If you get a message saying they have, don’t pay: call us, or check them here in your Mining Match account.'));
}

function linesTable(inv) {
  const totals = [['Subtotal', inv.subtotal_cents]];
  if (inv.gst_registered) totals.push(['GST (10%)', inv.gst_cents]);
  return h('table', { class: 'lines' },
    h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Description'),
      h('th', { scope: 'col', class: 'num' }, inv.gst_registered ? 'Amount (excl. GST)' : 'Amount'))),
    h('tbody', {}, inv.lines.map((l) => h('tr', {}, h('td', {}, l.description), h('td', { class: 'num' }, money(l.amount_cents))))),
    h('tfoot', {},
      totals.map(([k, v]) => h('tr', {}, h('th', { scope: 'row', class: 'num' }, k), h('td', { class: 'num' }, money(v)))),
      h('tr', { class: 'total-row' }, h('th', { scope: 'row', class: 'num' }, 'Total (AUD)'), h('td', { class: 'num' }, money(inv.total_cents)))));
}

function adminActions(ctx, inv) {
  const reload = () => ctx.reload('h1');
  const parts = [];
  if (inv.status === 'issued') {
    const paidOn = input({ type: 'date', value: isoDate(today()), max: isoDate(today()), required: true });
    const note = input({ maxlength: '200', placeholder: 'e.g. CBA transfer, ref MM-00001' });
    const error = errorArea();
    parts.push(onSubmit(h('form', { class: 'panel stack' }, h('h2', {}, 'Record the payment'),
      h('p', { class: 'small muted' }, 'Check the full amount has arrived in the bank account first.'),
      h('div', { class: 'grid-2' }, field('Date received', paidOn), field('Note (optional)', note)), error.el,
      h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, 'Mark as paid'))), error, async () => {
      await post(`/invoices/${inv.id}/paid`, { paid_on: paidOn.value, note: note.value || null });
      toast(`${inv.number} marked as paid.`);
      reload();
    }));
    parts.push(h('div', { class: 'panel stack' }, h('h2', {}, 'Remind them'),
      h('p', { class: 'small muted' }, inv.reminders_sent
        ? `${inv.reminders_sent} reminder${inv.reminders_sent === 1 ? '' : 's'} sent, last on ${fmtDate(inv.last_reminder_on)}.`
        : 'Emails the invoice again with a reminder. One a day at most.'),
      h('div', { class: 'actions' }, actionButton('Send reminder', async () => {
        await post(`/invoices/${inv.id}/remind`);
        toast('Reminder sent.');
        reload();
      }))));
  }
  if (inv.status === 'issued' || inv.status === 'scheduled') {
    const reason = input({ required: true, maxlength: '200', placeholder: 'e.g. Candidate didn’t start' });
    const error = errorArea();
    parts.push(h('div', { class: 'panel stack' }, h('h2', {}, 'Void this invoice'),
      h('p', { class: 'small muted' }, 'It keeps its number and stays on record. Issue a corrected one if needed.'),
      field('Reason', reason), error.el,
      h('div', { class: 'actions' }, confirmButton('Void invoice', 'Tap again to void', async () => {
        error.clear();
        if (!reason.value.trim()) { error.show(new Error('Give a reason first.')); return; }
        await post(`/invoices/${inv.id}/void`, { reason: reason.value });
        toast('Invoice voided.');
        reload();
      }))));
  }
  return parts.length ? h('div', { class: 'stack no-print' }, parts) : null;
}

export async function invoice(ctx) {
  const inv = await get(`/invoices/${ctx.params.id}`);
  const admin = ctx.me.role === 'admin';
  const back = h('a', { class: 'back no-print', href: admin ? '#/admin/invoices' : '#/billing' }, admin ? 'All invoices' : 'Back to billing');

  if (inv.status === 'scheduled' || (inv.status === 'void' && !inv.number)) {
    return h('div', { class: 'stack' }, back,
      h('div', { class: 'page-head' }, h('h1', {}, inv.status === 'void' ? 'Cancelled invoice' : 'Scheduled invoice'), statusBadge(inv)),
      inv.status === 'void'
        ? h('p', { class: 'lede' }, `Cancelled before it was issued. ${inv.void_reason || ''}`)
        : h('p', { class: 'lede' }, `This invoice will be issued and emailed on ${fmtDate(inv.issue_date)}, due ${fmtDate(inv.due_date)}.`),
      h('div', { class: 'invoice-doc' }, linesTable(inv)),
      admin ? adminActions(ctx, inv) : null);
  }

  const s = inv.seller; const b = inv.buyer;
  const doc = h('article', { class: 'invoice-doc', 'aria-labelledby': 'inv-title' },
    h('div', { class: 'invoice-head' },
      h('div', {}, h('p', { class: 'invoice-seller' }, s.legal_name), h('h1', { id: 'inv-title' }, `${inv.title} ${inv.number}`)),
      statusBadge(inv)),
    inv.status === 'void' ? h('p', { class: 'notice warn' }, `Void: ${inv.void_reason || 'cancelled'}. Don’t pay this invoice.`) : null,
    h('div', { class: 'invoice-parties' },
      h('div', {}, h('h2', {}, 'From'), h('p', {}, s.legal_name, h('br'), `ABN ${s.abn}`, h('br'), s.address, h('br'), s.email,
        s.phone ? [h('br'), s.phone] : null)),
      h('div', {}, h('h2', {}, 'Bill to'), h('p', {}, b.company_name, b.abn ? [h('br'), `ABN ${b.abn}`] : null,
        b.address ? [h('br'), b.address] : null, h('br'), b.email)),
      h('div', {}, h('h2', {}, 'Details'), facts([['Issued', fmtDate(inv.issue_date)], ['Due', fmtDate(inv.due_date)],
        ['Reference', inv.number]]))),
    linesTable(inv),
    h('p', { class: 'small muted' }, inv.gst_registered ? 'Total price includes GST.' : 'No GST has been charged.'),
    inv.status !== 'void' && inv.status !== 'paid' ? payBox(inv.payment, inv.number) : null,
    inv.status === 'paid' ? h('p', { class: 'notice ok' }, `Paid ${fmtDate(inv.paid_on)}${inv.paid_on_time ? ', on time' : ''}. Thank you.`,
      admin && inv.payment_note ? ` Note: ${inv.payment_note}` : '') : null,
    inv.kind === 'placement' && inv.guarantee_days !== null
      ? h('p', { class: 'small' }, `Replacement guarantee: ${inv.guarantee_days} days from the start date, provided this invoice is paid by the due date.`)
      : null);

  return h('div', { class: 'stack' }, back, doc,
    h('div', { class: 'actions no-print' },
      actionButton('Download PDF', () => download(`/invoices/${inv.id}/pdf`, `${inv.number}.pdf`), { class: 'btn primary' }),
      h('button', { type: 'button', class: 'btn', onclick: () => window.print() }, 'Print')),
    admin ? adminActions(ctx, inv) : null);
}
