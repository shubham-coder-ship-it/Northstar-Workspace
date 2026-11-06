'use strict';
const $ = (selector) => document.querySelector(selector);
const paper = document.body.dataset.mode === 'practice';
let csrf = $('meta[name=csrf-token]').content;
let user = null;
let registering = false;
let submission = null;
let workspace = null;
const resetToken = location.hash.startsWith('#reset=') ? location.hash.slice(7) : '';
let emailToken = location.hash.startsWith('#verify-email=') ? location.hash.slice(14) : '';
if (resetToken || emailToken) history.replaceState(null, '', location.pathname + location.search);
function notice(message, error=false) {
  const node = $('#notice'); node.textContent = message; node.classList.toggle('error', error); node.hidden = false;
}
async function api(path, {method='GET', body, headers={}}={}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 25000);
  try {
    const response = await fetch(path, {method, signal:controller.signal, credentials:'same-origin',
      headers:{...(method==='GET'?{}:{'X-CSRF-Token':csrf}), ...(body instanceof FormData?{}:{'Content-Type':'application/json'}), ...headers},
      body:body===undefined?undefined:body instanceof FormData?body:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) {
      if (response.status===401 && user) { user=null; $('#auth-dialog').showModal(); }
      throw new Error(data.error || 'Request could not be completed.');
    }
    return data;
  } catch(error) {
    if (error.name==='AbortError' && path === '/api/live/orders') throw new Error('Live order response timed out. Check Binance and Northstar live order history before resubmitting.');
    if (error.name==='AbortError') throw new Error(paper ? 'The request timed out. Refresh order history to check whether it was saved.' : 'The connection timed out. Refresh order history and check Binance before submitting another trade.');
    if (error instanceof TypeError) throw new Error('Connection lost. Check your network. Trading requests will not be automatically retried.');
    throw error;
  } finally { clearTimeout(timer); }
}
async function busy(button, fn) {
  button.disabled=true; const old=button.textContent; button.textContent='Working…';
  try { await fn(); } catch(error) { notice(error.message,true); } finally {button.disabled=false;button.textContent=old;}
}
function openRequestedPage() {
  if (!user || location.pathname !== '/admin') return;
  if (user.role === 'admin') tab('admin');
  else notice('Administrator access required.', true);
}
function tab(name) {
  for (const id of ['overview','history','settings','admin','welcome','professional']) $('#'+id).hidden=id!==name;
  for (const button of document.querySelectorAll('[data-tab]')) button.classList.toggle('active',button.dataset.tab===name);
  $('#page-label').textContent={welcome:'Profile',professional:'Live dashboard',overview:'Overview',history:'Order history',settings:'Account & billing',admin:'Administration'}[name];
  $('#notice').hidden = true;
  $('#mode-switch').hidden = !workspace;
  $('#mode-switch').textContent = workspace === 'professional' ? (paper ? 'Switch to Practice' : 'Switch to Testnet') : 'Switch to Live';
  if (name==='admin') loadAdmin();
  if (name==='professional') loadLive().catch(error => notice(error.message, true));
}
for (const button of document.querySelectorAll('[data-tab]')) button.addEventListener('click',()=>tab(button.dataset.tab === 'overview' ? (workspace || 'welcome') :
    workspace === 'professional' && button.dataset.tab === 'history' ? 'professional' : button.dataset.tab));
$('#account-button').addEventListener('click',()=>user?tab('settings'):$('#auth-dialog').showModal());
$('#auth-dialog').addEventListener('cancel',(event)=>event.preventDefault());
$('#auth-toggle').addEventListener('click',()=>{
  registering=!registering;
  for (const id of ['register-name','register-confirm']) {
    const field = $('#'+id);
    field.hidden = !registering;
    field.firstElementChild.disabled = !registering;
    field.firstElementChild.required = registering;
  }
  $('#forgot-password').hidden = registering;
  $('#auth-title').textContent=registering?'Make room for clarity.':'Welcome back.';
  $('#auth-submit').textContent=registering?'Create account ↗':'Sign in ↗';
  $('#auth-toggle').textContent=registering?'Have an account? Sign in':'New here? Create an account';
  $('#auth-form [name=password]').autocomplete=registering?'new-password':'current-password';
  $('#auth-error').textContent='';
});
$('#auth-form').addEventListener('submit',async(event)=>{
  event.preventDefault();const button=$('#auth-submit');button.disabled=true;$('#auth-error').textContent='';
  try {
    const result=await api(registering?'/api/register':'/api/login',{method:'POST',body:Object.fromEntries(new FormData(event.target))});
    event.target.reset();
    if (registering) {
      $('#auth-toggle').click();
      $('#auth-error').textContent = 'Account created. Sign in to continue.';
      return;
    }
    user=result.user;csrf=result.csrf;$('#auth-dialog').close();
    workspace = null;
    tab('welcome');
    await refresh();
    openRequestedPage();
    await verifyEmail();
  } catch(error) {$('#auth-error').textContent=error.message;} finally {button.disabled=false;}
});
for (const button of document.querySelectorAll('[data-toggle-password]')) {
  button.addEventListener('click', () => {
    const input = button.form.elements[button.dataset.togglePassword];
    input.type = input.type === 'password' ? 'text' : 'password';
    button.textContent = input.type === 'password' ? 'Show password' : 'Hide password';
  });
}
function selectMode(mode) {
  workspace = mode;
  tab(mode);
  const label = mode === 'professional' ? 'Live Mode' : paper ? 'Practice Mode' : 'Testnet';
  $('.badge').textContent = label;
  document.body.classList.toggle('professional-mode', mode === 'professional');
}
$('#choose-practice').addEventListener('click', () => selectMode('overview'));
$('#choose-professional').addEventListener('click', () => selectMode('professional'));
$('#mode-switch').addEventListener('click', () => selectMode(workspace === 'overview' ? 'professional' : 'overview'));
$('#edit-profile').addEventListener('click', () => tab('settings'));
$('#signout').addEventListener('click', () => {
  $('#signout-dialog').returnValue = 'cancel';
  $('#signout-dialog').showModal();
});
$('#signout-dialog').addEventListener('close', () => {
  if ($('#signout-dialog').returnValue === 'signout') {
    busy($('#signout'), async () => { await api('/api/logout', {method:'POST', body:{}}); location.reload(); });
  }
});
$('#profile-form').addEventListener('submit', event => {
  event.preventDefault();
  busy(event.submitter, async () => {
    await api('/api/profile', {method:'POST', body:Object.fromEntries(new FormData(event.target))});
    await refresh();
    notice('Profile saved.');
  });
});
$('#email-form').addEventListener('submit', event => {
  event.preventDefault();
  busy(event.submitter, async () => {
    await api('/api/profile/email', {method:'POST', body:Object.fromEntries(new FormData(event.target))});
    event.target.reset();
    notice('Open the verification link sent to your new email address.');
  });
});
async function verifyEmail() {
  if (!user || !emailToken) return;
  const token = emailToken;
  emailToken = '';
  try {
    await api('/api/profile/email/verify', {method:'POST', body:{token}});
    user = null;
    await refresh();
    $('#auth-error').textContent = 'Email updated. Sign in with your new address.';
  } catch (error) {
    notice(error.message, true);
  }
}
function openRecovery() {
  if ($('#auth-dialog').open) $('#auth-dialog').close();
  const form = $('#recovery-form');
  $('#recovery-email').hidden = Boolean(resetToken);
  form.elements.email.disabled = Boolean(resetToken);
  $('#recovery-fields').hidden = !resetToken;
  for (const name of ['new', 'confirm']) {
    form.elements[name].disabled = !resetToken;
    form.elements[name].required = Boolean(resetToken);
  }
  $('#recovery-dialog').showModal();
}
$('#forgot-password').addEventListener('click', openRecovery);
$('#recovery-back').addEventListener('click', () => location.reload());
$('#recovery-dialog').addEventListener('cancel', event => { event.preventDefault(); location.reload(); });
$('#recovery-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  const status = $('#recovery-status');
  try {
    const body = Object.fromEntries(new FormData(event.target));
    if (resetToken) body.token = resetToken;
    await api(resetToken ? '/api/password/reset' : '/api/password/forgot', {method:'POST',body});
    status.textContent = resetToken ? 'Password updated. Return to sign in.' : 'If the account exists, a reset link will be sent.';
    if (resetToken) event.target.reset();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
function element(tag, text, className) {const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
let recentOrders = [];
$('#pair-filter')?.addEventListener('input', () => renderOrders(recentOrders));
function renderOrders(orders) {
  recentOrders = orders;
  const filter = $('#pair-filter')?.value.trim().toUpperCase() || '';
  const visible = orders.filter(order => order.symbol.includes(filter));
  $('#order-count').replaceChildren(document.createTextNode(String(orders.length)+' '),element('small','orders'));
  const body=$('#history-body');body.replaceChildren();$('#history-empty').hidden=visible.length>0;
  for(const order of visible) {
    const row=element('tr');
    for(const text of [order.symbol,order.side+' / '+order.kind,order.quantity,...(paper ? [order.fill_price ?? '—', order.side === 'SELL' ? (order.pnl ?? '—') : '—'] : []),order.status,new Date(order.created*1000).toLocaleString()])row.append(element('td',text));
    body.append(row);
  }
  const recent=$('#recent-list');
  if(orders.length){recent.className='';recent.replaceChildren();for(const order of orders.slice(0,4)){
    const row=element('div',undefined,'activity-row');const details=element('div',order.symbol+' · '+order.side);details.append(element('small',order.kind+' · '+order.quantity));
    row.append(details,element('span',order.status,'pill'));recent.append(row);
  }}
}
let liveLoad = 0;
async function loadLive() {
  const load = ++liveLoad;
  $('#live-ticket').hidden = true;
  $('#live-state').textContent = 'Loading live account…';
  const result = await api('/api/live/account');
  if (load !== liveLoad) return;
  $('#live-credentials').hidden = result.connected;
  $('#live-disconnect').hidden = !result.connected;
  $('#live-ticket').hidden = !result.can_trade;
  $('#live-state').textContent = result.error || (result.connected
    ? (result.can_trade ? 'Live account connected.' : 'Connected. This account is not enabled to trade.')
    : 'Connect a live Binance Futures API key to continue.');
  const balances = $('#live-balances');
  balances.replaceChildren();
  for (const asset of result.assets || []) {
    for (const [label, field] of [['USDT wallet balance','walletBalance'],['Available USDT','availableBalance'],['USDT unrealized P/L','unrealizedProfit']]) {
      const cell = element('div');
      cell.append(element('dt', label), element('dd', asset[field] ?? 'Unavailable'));
      balances.append(cell);
    }
  }
  const rows = result.orders.map(order => {
    const row = element('tr');
    for (const value of [order.symbol, order.side + ' / ' + order.kind, order.quantity, order.status,
      order.exchange_id ?? 'ns-' + order.id, new Date(order.created * 1000).toLocaleString()]) {
      row.append(element('td', value));
    }
    const cell = element('td');
    if (result.connected && ['UNKNOWN','SUBMITTING','SUBMITTED','NEW','PARTIALLY_FILLED'].includes(order.status)) {
      const check = element('button', 'Check Binance', 'text-button');
      check.addEventListener('click', () => busy(check, async () => {
        await api('/api/live/orders/' + order.id + '/status', {method:'POST', body:{}});
        await loadLive();
      }));
      cell.append(check);
    }
    row.append(cell);
    return row;
  });
  $('#live-history').replaceChildren(...rows);
  $('#live-empty').hidden = rows.length > 0;
}
$('#live-refresh').addEventListener('click', event => busy(event.currentTarget, loadLive));
$('#live-credentials').addEventListener('submit', event => {
  event.preventDefault();
  busy(event.submitter, async () => {
    await api('/api/live/credentials', {method:'POST', body:Object.fromEntries(new FormData(event.target))});
    event.target.reset();
    await loadLive();
  });
});
$('#live-disconnect').addEventListener('click', event => busy(event.currentTarget, async () => {
  await api('/api/live/credentials', {method:'DELETE', body:{}});
  await loadLive();
}));
const liveOrder = $('#live-order');
const liveReview = $('#live-review');
const liveConfirm = $('#live-confirm');
let liveDraft = null;
let liveKey = null;
liveOrder.addEventListener('input', () => { liveKey = null; });
liveOrder.elements.kind.addEventListener('change', () => {
  const limit = liveOrder.elements.kind.value === 'LIMIT';
  $('#live-price').hidden = !limit;
  liveOrder.elements.price.disabled = !limit;
  liveOrder.elements.price.required = limit;
});
liveOrder.addEventListener('submit', event => {
  event.preventDefault();
  liveDraft = Object.fromEntries(new FormData(liveOrder));
  $('#live-review-summary').textContent = liveDraft.side + ' ' + liveDraft.quantity + ' ' + liveDraft.symbol
    + (liveDraft.kind === 'LIMIT' ? ' at ' + liveDraft.price + ' USDT (limit)' : ' at market price');
  liveConfirm.reset();
  liveReview.returnValue = 'cancel';
  liveReview.showModal();
});
liveReview.addEventListener('close', () => {
  if (liveReview.returnValue !== 'send' || liveConfirm.elements.confirmation.value !== 'LIVE') return;
  const key = liveKey ||= crypto.randomUUID().replaceAll('-', '');
  busy(liveOrder.querySelector('button'), async () => {
    try {
      const result = await api('/api/live/orders', {method:'POST', headers:{'Idempotency-Key':key},
        body:{...liveDraft, confirmation:'LIVE'}});
      notice('Live order status: ' + result.status);
    } finally {
      await loadLive().catch(() => {});
    }
  });
});
async function refreshPractice() {
  const stats = await api('/api/practice/analytics');
  const usd = value => value == null ? '—' : Number(value).toLocaleString(undefined, {maximumFractionDigits: 4}) + ' USDT';
  const metrics = [
    ['Virtual cash', usd(stats.balance)], ['Filled trades', stats.total],
    ['Buys', stats.sides.BUY.count], ['Sells', stats.sides.SELL.count],
    ['Average trade value', usd(stats.average)], ['Realized P/L', usd(stats.pnl)],
    ['Smallest buy', usd(stats.sides.BUY.low)], ['Largest buy', usd(stats.sides.BUY.high)],
    ['Smallest sell', usd(stats.sides.SELL.low)], ['Largest sell', usd(stats.sides.SELL.high)],
    ['Winning sells', stats.wins], ['Losing sells', stats.losses],
  ];
  $('#practice-stats').replaceChildren(...metrics.map(([label, value]) => {
    const cell = element('div');
    cell.append(element('dt', label), element('dd', String(value)));
    return cell;
  }));
  const canvas = $('#practice-chart');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  canvas.hidden = !stats.series.length;
  $('#practice-chart-caption').textContent = stats.series.length
    ? 'Virtual cash after each recent fill. Holdings are tracked separately.'
    : 'Your cash chart will appear after your first fill.';
  if (!stats.series.length) return;
  const balances = [Number(stats.initial), ...stats.series.map(point => Number(point.balance))];
  const min = Math.min(...balances), span = Math.max(...balances) - min || 1;
  ctx.beginPath();
  ctx.strokeStyle = '#edbd68';
  ctx.lineWidth = 3;
  balances.forEach((value, index) => {
    const x = 8 + index * (canvas.width - 16) / (balances.length - 1);
    const y = canvas.height - 8 - (value - min) * (canvas.height - 16) / span;
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}
async function refresh() {
  const result=await api('/api/session');user=result.user;csrf=result.csrf;
  if(!user){if(!$('#auth-dialog').open && !$('#recovery-dialog').open)$('#auth-dialog').showModal();return;}
  $('#admin-nav').hidden=user.role!=='admin';$('#signout').hidden=false;
  $('#account-button').textContent=user.email[0].toUpperCase();
  if (!paper) $('#connection-status').textContent=user.connected?'Credentials saved':'Not connected';
  $('#membership').textContent=user.subscription==='none'?'Free workspace':user.subscription;
  $('#billing-status').textContent='Subscription status: '+user.subscription;
  $('#billing-plan').textContent = user.subscription === 'none' ? 'Plan: Free workspace' : 'Plan: Northstar membership';
  $('#welcome-name').textContent = user.full_name || 'Name not provided';
  $('#welcome-email').textContent = user.email;
  $('#welcome-age').textContent = user.age ? 'Age: ' + user.age : 'Age not provided';
  $('#profile-form').elements.full_name.value = user.full_name;
  $('#profile-form').elements.age.value = user.age ?? '';
  $('#profile-email').value = user.email;
  if(user.avatar){const image=element('img');image.src='/api/profile/image?v='+Date.now();image.alt='Your profile';$('#profile-preview').replaceChildren(image);$('#welcome-photo').replaceChildren(image.cloneNode());}
  renderOrders((await api('/api/orders')).orders);
  if (paper) await refreshPractice();
  const plan = await api('/api/billing/plan');
  if (plan.label) $('#billing-plan').textContent += ' · Current offer: ' + plan.label;
  $('#member-export').hidden = !plan.member_export;
}
$('#refresh').addEventListener('click',()=>busy($('#refresh'),refresh));
$('#order-form [name=kind]').addEventListener('change',(event)=>{
  const limit=event.target.value==='LIMIT';$('#price-field').hidden=!limit;$('#order-form [name=price]').required=limit;
  $('#execution-label').textContent=limit?'Your limit price':paper?'Market order':'Market price';
});
$('#order-form').addEventListener('input',()=>{submission=null;});
$('#order-form').addEventListener('submit',(event)=>{
  event.preventDefault();
  busy(event.submitter,async()=>{
    if(!user)throw new Error('Please sign in first.');
    if(!paper && !user.connected)throw new Error('Connect your testnet API credentials in Account & billing first.');
    const data=Object.fromEntries(new FormData(event.target));data.confirmed=data.confirmed==='on';
    if(!submission)submission=crypto.randomUUID().replaceAll('-','');
    const result=await api('/api/orders',{method:'POST',body:data,headers:{'Idempotency-Key':submission}});
    notice(paper ? 'Practice order status: '+result.status.replaceAll('_', ' ').toLowerCase()+'. Review your order history.' : 'Testnet order status: '+result.status+'. Review your order history.');
    // Keep the same key until the user edits the form, including after ambiguous responses.
    await refresh();
  });
});
$('#password-form').addEventListener('submit',(event)=>{event.preventDefault();busy(event.submitter,async()=>{await api('/api/password',{method:'POST',body:Object.fromEntries(new FormData(event.target))});sessionStorage.setItem('northstar-signin-message', 'Password changed. Sign in with your new password.');location.reload();});});
$('#credentials-form')?.addEventListener('submit',(event)=>{event.preventDefault();busy(event.submitter,async()=>{
  await api('/api/credentials',{method:'POST',body:Object.fromEntries(new FormData(event.target))});event.target.reset();await refresh();notice('Your testnet credentials were encrypted and saved. Connection is verified when you submit an order.');
});});
$('#disconnect')?.addEventListener('click',()=>busy($('#disconnect'),async()=>{await api('/api/credentials',{method:'DELETE',body:{}});await refresh();notice('Exchange credentials removed.');}));
$('#image-form').addEventListener('submit',(event)=>{event.preventDefault();busy(event.submitter,async()=>{
  const file=event.target.elements.image.files[0];if(!file || file.size>2*1024*1024)throw new Error('Choose an image smaller than 2 MB.');
  await api('/api/profile/image',{method:'POST',body:new FormData(event.target)});event.target.reset();await refresh();notice('Profile image updated.');
});});
for(const [id,path] of [['subscribe','checkout'],['billing-portal','portal']])$('#'+id).addEventListener('click',()=>busy($('#'+id),async()=>{
  const result=await api('/api/billing/'+path,{method:'POST',body:{}});
  const target=new URL(result.url);if(target.protocol!=='https:' || !['checkout.stripe.com','billing.stripe.com'].includes(target.hostname))throw new Error('Unexpected payment destination.');
  location.assign(target.href);
}));
let adminSection = 'overview';
let adminData;
const adminContent = $('#admin-content');
const dateLabel = seconds => new Date(seconds * 1000).toLocaleString();

function adminTable(columns, rows) {
  if (!rows.length) return element('p', 'No records to show.', 'muted');
  const wrapper = element('div', undefined, 'table-wrap');
  const table = element('table');
  const header = table.createTHead().insertRow();
  columns.forEach(label => header.append(element('th', label)));
  const body = table.createTBody();
  for (const cells of rows) {
    const row = body.insertRow();
    for (const value of cells) {
      const cell = row.insertCell();
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value ?? '—';
    }
  }
  wrapper.append(table);
  return wrapper;
}
function adminMetrics(pairs) {
  const list = element('dl', undefined, 'practice-stats');
  for (const [label, value] of pairs) {
    const card = element('div');
    card.append(element('dt', label), element('dd', String(value)));
    list.append(card);
  }
  return list;
}
function adminAction(label, path, body, prompt) {
  const button = element('button', label, 'text-button');
  button.addEventListener('click', () => {
    if (!window.confirm(prompt)) return;
    busy(button, async () => { await api(path, {method:'POST',body}); await loadAdmin(); });
  });
  return button;
}
function activityRows(events) {
  return events.map(event => [dateLabel(event.created), event.actor ?? 'System', event.action, event.target]);
}
function tradeRows(orders) {
  return orders.map(order => [dateLabel(order.created), order.user_id, order.mode,
    order.symbol, order.side, order.kind, order.quantity, order.price, order.status]);
}
function renderAdmin() {
  if (!adminData) return;
  const {counts, users, orders, events, subscriptions, growth} = adminData;
  const active = document.querySelector('[data-admin="' + adminSection + '"]');
  $('#admin-title').textContent = active.textContent;
  for (const button of document.querySelectorAll('[data-admin]')) button.classList.toggle('active', button === active);
  $('#admin-search-label').hidden = adminSection !== 'users';
  adminContent.replaceChildren();
  const show = (...nodes) => adminContent.append(...nodes);
  const audits = ['Date','Actor ID','Action','Target ID'];
  const trades = ['Date','User ID','Mode','Pair','Side','Type','Quantity','Limit','Status'];
  if (adminSection === 'overview') {
    show(adminMetrics([
      ['Registered users',counts.users],['Enabled accounts',counts.enabled],['Signed in · last 7 days',counts.recent_users],
      ['New registrations · last 7 days',counts.registrations],['Practice accounts',counts.practice_accounts],
      ['Connected Live accounts',counts.live_accounts],['Submitted orders',counts.orders],
      ['Filled orders',counts.filled_trades],['Buy orders',counts.buys],['Sell orders',counts.sells],
      ['Practice volume · USDT',counts.practice_volume],['Active subscriptions',counts.subscribers],
      ['Rejected or uncertain orders',counts.failed],['Monthly recurring revenue','Unavailable'],
    ]), element('h3','Recent platform activity'), adminTable(audits,activityRows(events.slice(0,10))));
  } else if (adminSection === 'users') {
    const query = $('#admin-search').value.trim().toLowerCase();
    show(adminTable(['ID','Full name','Email','Age','Registered','Role','Access','Mode usage','Subscription','Actions'],
      users.filter(user => [user.id,user.full_name,user.email].some(value => String(value ?? '').toLowerCase().includes(query)))
      .map(user => {
        const controls = element('div');
        const activity = element('button','Activity','text-button');
        activity.addEventListener('click', () => {
          $('#admin-user-title').textContent = user.full_name || user.email;
          $('#admin-user-details').replaceChildren(adminTable(audits,activityRows(events.filter(e => e.actor === user.id || e.target === user.id))),
            adminTable(trades,tradeRows(orders.filter(o => o.user_id === user.id))));
          $('#admin-user-dialog').showModal();
        });
        controls.append(activity);
        if (user.role === 'user') controls.append(adminAction(user.disabled ? 'Reactivate' : 'Suspend',
          '/api/admin/users/'+user.id+'/access', {disabled:!user.disabled},
          (user.disabled ? 'Reactivate ' : 'Suspend ') + user.email + '?'));
        return [user.id,user.full_name,user.email,user.age,dateLabel(user.created),user.role,user.disabled ? 'Suspended' : 'Active',
          [user.practice && 'Practice',user.live_connected && 'Live'].filter(Boolean).join(' / ') || 'No recorded usage',
          user.subscription_status,controls];
      })));
  } else if (adminSection === 'trades') {
    show(element('p','Latest 200 submissions across Practice, Live, and Testnet.','muted'),adminTable(trades,tradeRows(orders)));
  } else if (adminSection === 'practice') {
    show(adminMetrics([['Practice accounts',counts.practice_accounts],['Starting balance · USDT',adminData.practice_start],
      ['Virtual fills',counts.practice_fills],['Virtual volume · USDT',counts.practice_volume],['Realized P/L · USDT',counts.practice_pnl]]),
      adminTable(['ID','Account','Virtual balance reset'],users.filter(u => u.practice).map(u => [u.id,u.email,
        adminAction('Reset account','/api/admin/practice/'+u.id+'/reset',{confirm:'RESET'},
          'Reset virtual cash and holdings for '+u.email+'? Historical records are retained.')])));
  } else if (adminSection === 'live') {
    show(element('p','Saved connections. Exchange availability is checked from each user’s Live Mode.','muted'),
      adminTable(['ID','Account','Connection','Verification'],users.filter(u => u.live_connected)
        .map(u => [u.id,u.email,'Credentials saved','Verified when connected'])),
      element('h3','Recent Live orders'),adminTable(trades,tradeRows(orders.filter(o => o.mode === 'Live'))));
  } else if (adminSection === 'billing') {
    show(adminMetrics([['Stripe configuration',adminData.stripe_configured ? 'Configured' : 'Unavailable'],
      ['Active subscriptions',counts.subscribers],['Monthly recurring revenue','Unavailable']]),
      adminTable(['Subscription status','Accounts'],subscriptions.map(row => [row.status,row.total])),
      element('p','Payment history, refunds and recurring revenue require Stripe billing records.','muted'));
    const form = element('form');
    const label = element('label','Recurring Stripe Price ID');
    const input = element('input');
    input.required = true; input.value = adminData.billing_price || '';
    label.append(input);
    const save = element('button','Set subscription price','primary');
    form.append(label,save);
    form.addEventListener('submit', event => {
      event.preventDefault();
      busy(save, async () => {
        await api('/api/admin/billing/price', {method:'POST',body:{price_id:input.value.trim()}});
        await loadAdmin();
        notice('Subscription price updated.');
      });
    });
    show(element('p',adminData.billing_label || 'Create a recurring price in Stripe, then enter its Price ID here.','muted'),form);
  } else if (adminSection === 'audit') {
    show(element('p','Latest 200 recorded events.','muted'),adminTable(audits,activityRows(events)));
  } else if (adminSection === 'reports') {
    const download = element('button','Export growth CSV','secondary');
    download.addEventListener('click', () => {
      const csv = ['Date,Registrations',...growth.map(row => row.day+','+row.registrations)].join(String.fromCharCode(10));
      const url = URL.createObjectURL(new Blob([csv], {type:'text/csv'}));
      const link = element('a'); link.href = url; link.download = 'northstar-registrations.csv'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    show(element('p','Daily registrations · latest 30 days · UTC','muted'),download,
      adminTable(['Date','Registrations'],growth.map(row => [row.day,row.registrations])));
  } else {
    show(element('p','Virtual starting balance · USDT','muted'));
    if (!adminData.super_admin) {
      show(element('p','A Super Admin can change the starting balance. Current setting: '+adminData.practice_start+' USDT.'));
    } else {
      const form = element('form');
      const label = element('label','Starting balance');
      const input = element('input'); input.value = adminData.practice_start; input.required = true; input.inputMode = 'decimal';
      const submit = element('button','Save starting balance','primary');
      label.append(input); form.append(label,submit);
      form.addEventListener('submit',event => {
        event.preventDefault();
        busy(submit,async () => { await api('/api/admin/settings/practice',{method:'POST',body:{balance:input.value}}); await loadAdmin(); });
      });
      show(form);
    }
  }
}
async function loadAdmin() {
  $('#admin-message').textContent = 'Loading platform data…';
  try {
    adminData = await api('/api/admin/overview');
    renderAdmin();
    $('#admin-message').textContent = '';
  } catch(error) { $('#admin-message').textContent = error.message; }
}
for (const button of document.querySelectorAll('[data-admin]')) button.addEventListener('click', () => {
  adminSection = button.dataset.admin;
  renderAdmin();
});
$('#admin-search').addEventListener('input',renderAdmin);
$('#admin-refresh').addEventListener('click',loadAdmin);
$('#workspace-practice').addEventListener('click', () => { if (user) selectMode('overview'); });
$('#workspace-live').addEventListener('click', () => { if (user) selectMode('professional'); });
window.addEventListener('offline',()=>{if (!paper) notice('You are offline. Wait for your connection before trading. Uncertain orders must be checked on Binance.',true);});
tab('welcome');
const signinMessage = sessionStorage.getItem('northstar-signin-message');
if (signinMessage) {
  $('#auth-error').textContent = signinMessage;
  sessionStorage.removeItem('northstar-signin-message');
}
refresh().then(() => { if (resetToken) openRecovery(); else { openRequestedPage(); return verifyEmail(); } }).catch(error=>notice(error.message,true));

