'use strict';
const $ = (id) => document.getElementById(id);
const SUITS = {S:'♠',H:'♥',C:'♣',D:'♦',J:''};
const SUIT_NAMES = {S:'黑桃',H:'红桃',C:'梅花',D:'方片',J:''};
const PHASES = {waiting:'等待准备',dealing:'发牌亮2',first_no_trump:'重新定庄',draw_trump:'抽底定主',declaring:'确认亮反',tribute_give:'进贡',tribute_return:'还贡',take_bottom:'庄家拿底',discard:'庄家扣底',playing:'出牌',settled:'本局结算'};
const KINDS = {single:'单张',throw:'甩牌',true_gang:'真杠',false_gang:'假杠'};
const LABELS = {ready:'准备开始',unready:'取消准备',change_seat:'换到空座',call_trump:'亮2定主',reveal:'亮反',confirm:'确认继续',draw_trump:'抽底定主',give_tribute:'进贡',return_tribute:'还贡',take_bottom:'拿起底牌',discard:'扣下6张',play:'出牌',next_deal:'下一局'};
let state = null, selected = new Set(), busy = false, polling = false, handKey = '', eventsKey = '', playKey = '', actionKey = '';
const friends = location.pathname === '/friends';
const api = friends ? '/api/friends' : '/api';
document.body.classList.toggle('friend-mode',friends);
document.querySelector('.brand').href=friends?'/friends':'/';
let requestEpoch = 0;
let specialKey = '', socialCursor = 0, playerTarget = null;
LABELS.revolution = '革命重开';
let historyKey = '', chatKey = '', chatRoom = '', chatCursor = 0, chatUnread = 0, chatSending = false;
let clockReceived = 0, voteStartKind = null, reportCursor = null, matchKey = '';
const EMOTES = {egg:'🥚',tomato:'🍅',flower:'🌸',clap:'👏'};
const EMOTE_LABELS = {egg:'扔鸡蛋',tomato:'扔西红柿',flower:'送花',clap:'鼓掌'};
try {$('mute-emotes').checked=localStorage.getItem('sanwufan-mute-emotes')==='1';} catch {}
const me = () => state.player_seat;
const ourTeam = () => me() % 2;
const relativeSeat = seat => (seat - me() + 4) % 4;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function name(seat) { return state?.players.find(p => p.seat === seat)?.name || '玩家'; }
function rankLabel(rank) { return rank === 'small' ? '小王' : rank === 'big' ? '大王' : rank; }
function cardLabel(id) { const [s,r] = id.split(':'); return SUIT_NAMES[s] + rankLabel(r); }
function publicReflection(id){return (state.reflections||[]).some(r=>r.cards.includes(id))?{trump:true,reflected:true}:null;}
function cardNode(id, mini = false, detail = null, interactive = false) {
  const [suit,rank] = id.split(':');
  const red = suit === 'H' || suit === 'D' || rank === 'big';
  const node = element(interactive ? 'button' : 'div', `card${red?' red':''}${suit==='J'?' joker':''}${mini?' mini':''}${detail?.exchanged?' exchanged':''}`);
  const corner = element('span','card-corner',suit === 'J' ? rankLabel(rank) : rank);
  if (suit !== 'J') corner.append(element('small','',SUITS[suit]));
  node.append(corner, element('span','card-center',suit === 'J' ? '✦' : SUITS[suit]));
  const reverse = corner.cloneNode(true); reverse.classList.add('reverse'); node.append(reverse);
  if (detail?.trump || detail?.exchanged) node.append(element('span','card-badge',detail.reflected?'反':detail.exchanged?'贡':'主'));
  if (interactive) {
    node.type = 'button'; node.dataset.card = id;
    node.setAttribute('aria-label', cardLabel(id) + (detail?.reflected?'，'+(rank==='3'?'三反':'五反'):detail?.trump?'，主牌':'') + (detail?.points?`，${detail.points}分`:'') + (detail?.exchanged?'，贡牌交换所得':''));
    node.setAttribute('aria-pressed','false');
    node.addEventListener('click', () => { if (selected.has(id)) selected.delete(id); else selected.add(id); feedback(''); renderSelection(); });
  } else node.setAttribute('aria-label',cardLabel(id)+(detail?.reflected?'，'+(rank==='3'?'三反':'五反'):''));
  return node;
}
function feedback(message, notice = false) {
  $('feedback').textContent = message; $('feedback').hidden = !message;
  $('feedback').classList.toggle('notice',notice);
  if($('room-dialog').open){$('room-feedback').textContent=message;$('room-feedback').hidden=!message;}
}
function showDialog(id){for(const d of document.querySelectorAll('dialog[open]'))d.close();$(id).showModal();}
function fitHand(){
  const hand=$('hand'),n=hand.children.length;
  if(!n||hand.clientWidth<=0||getComputedStyle(hand).display==='grid')return;
  const width=hand.children[0].getBoundingClientRect().width;
  const gap=n>1?Math.max(Math.max(24,width/2+2)-width,Math.min(8,(hand.clientWidth-8-width*n)/(n-1))):0;
  hand.style.setProperty('--hand-gap',`${gap}px`);
}
new ResizeObserver(fitHand).observe($('hand'));
function connection(online) {
  $('connection').className = `connection ${online?'online':'offline'}`;
  $('connection').textContent = online?'已连接':'连接中断，正在重试';
}
function accept(next) {
  if(friends) {
    const lobby=next.mode==='lobby';
    $('lobby').hidden=!lobby;$('game-layout').hidden=lobby;$('room-bar').hidden=lobby;
    document.body.classList.toggle('lobby-mode',lobby);
    $('room-open').hidden=lobby;
    $('chat-open').hidden=lobby;
    $('auto').hidden=$('auto-menu').hidden=true;
    $('reset').hidden=lobby || !next.is_owner;
    document.querySelector('footer span').textContent='四人好友房';
    if(lobby) {const wasSeated=Boolean(state);state=null;document.body.classList.remove('in-room');selected.clear();handKey=eventsKey=playKey=actionKey=specialKey='';if(!$('nickname').value)$('nickname').value=next.name||'';if(next.notice)$('lobby-feedback').textContent=next.notice;if(wasSeated)for(const d of document.querySelectorAll('dialog[open]'))d.close();$('history-open').disabled=true;document.title='三五反 · 好友与练习牌桌';connection(true);return;}
    $('room-title').textContent=`房间 ${next.room_code} · ${next.players.filter(p=>p.occupied).length}/4人`;
    $('recovery-code').value=next.recovery_code||'';
    const invite=new URL('/friends',location.origin);invite.searchParams.set('room',next.room_code);$('invite-link').value=invite.href;
    $('leave-room').disabled=busy || !['waiting','settled'].includes(next.phase);
    document.querySelector('footer span').textContent='四人好友房';
  }
  const sameTable=state && state.table_id===next.table_id;
  if (sameTable && next.version < state.version) return;
  if(state && state.room_code===next.room_code && (next.chat_sequence||0)<(state.chat_sequence||0))next={...next,chat_sequence:state.chat_sequence,chat_messages:state.chat_messages};
  const changed = !sameTable || state.version !== next.version;
  if(!sameTable || state.player_seat!==next.player_seat) {selected.clear();handKey=eventsKey=playKey=actionKey=specialKey='';$('kind').value='';socialCursor=next.social_sequence||0;}
  state = next; clockReceived=performance.now(); connection(true);document.body.classList.add('in-room');document.body.dataset.phase=state.phase;
  if (changed || friends) {
    selected = new Set([...selected].filter(id => state.hand.includes(id)));
    if (state.auto) selected.clear();
    render();
  }
  renderSocial();renderChat();renderSession();
}
async function poll() {
  if (polling) return;
  polling = true; let delay = 400;
  const epoch=requestEpoch;
  try { const response = await fetch(`${api}/state`,{cache:'no-store'}); if(!response.ok) throw new Error('连接失败'); const result=await response.json();if(epoch===requestEpoch)accept(result); }
  catch { connection(false); delay = 2000; }
  finally { polling = false; setTimeout(poll,delay); }
}
async function action(type) {
  if (!state || busy) return;
  busy = true; feedback(''); renderSelection();
  const withCards = ['call_trump','reveal','give_tribute','return_tribute','discard','play'].includes(type);
  const payload = {action:type,version:state.version,table_id:state.table_id,cards:withCards?[...selected]:[]};
  if(type === 'play' && !state.current_lead && $('kind').value) payload.kind = $('kind').value;
  try {
    const response = await fetch(`${api}/action`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok) { if(result.state) accept(result.state); feedback(result.error?.message || '操作失败，请重试'); }
    else { selected.clear(); $('kind').value = ''; accept(result); }
  } catch { feedback('连接中断，操作结果尚未确认。恢复连接后请查看牌桌。'); connection(false); }
  finally { busy = false; renderSelection(); }
}

function instruction() {
  if (state.fault) return state.fault;
  if (state.recovering) return '服务已恢复，手牌已保留。等待四位玩家用原浏览器重新连接后继续发牌。';
  if (state.auto) return '本局托管中。点击“接手本局”即可恢复自己操作。';
  const a = state.available_actions;
  if (state.phase === 'waiting') return a.includes('ready') ? (friends?'邀请朋友入座，四人准备后开始发牌。对面是你的搭档。':'三位牌友已就座。准备好后开始发牌。') : '你已准备，等待其他玩家准备。';
  if (state.phase === 'dealing') return state.called_two ? `${name(state.caller)}已亮2，${SUIT_NAMES[state.trump_suit]}为主。等待发完手牌。` : state.auto_first_trump&&state.deal_number===1 ? '正在发牌，首局最先收到的2自动亮主。三五反会在手牌发完后判断。' : state.dealt_count === 48 ? `手牌已发完，保留${friends?5:3}秒亮2窗口。选一张2，点击“亮2定主”。` : '正在慢速发牌。拿到2后，选中它并点击“亮2定主”。三五反会在手牌发完后判断。';
  if (state.phase === 'draw_trump') return a.includes('draw_trump') ? '无人亮2，闲家抽底定主。抽到王时重抽。' : '等待闲家抽底确定主花色。';
  if (state.phase === 'first_no_trump') return '首局无人亮2，重新发牌。';
  if (state.phase === 'declaring') return a.includes('confirm') ? '三张3或5已自动亮反；四张可选择亮法或保留真杠。确认后继续进贡，庄家扣牌前仍可补亮反。' : '你已确认，等待其他玩家确认。庄家扣牌前仍可补亮反。';
  if (state.phase === 'tribute_give') return a.includes('give_tribute') ? '选一张最大的主牌进贡，方片5也可以进贡。' : '等待需要进贡的玩家选贡牌。';
  if (state.phase === 'tribute_return') return a.includes('return_tribute') ? '选一张不带分的主牌还贡；不能还已亮的三反牌，也不能原样返还收到的贡牌。' : '等待收贡玩家还贡。';
  if (state.phase === 'take_bottom') return a.includes('take_bottom') ? '进贡还贡已处理完毕。你是庄家，请拿起6张底牌。' : `等待庄家${name(state.dealer)}拿底。`;
  if (state.phase === 'discard') return a.includes('discard') ? '扣牌前可用底牌中的3、5亮反。选6张扣底，不能扣任何5、10、K；扣牌提交后停止亮反。' : `等待庄家${name(state.dealer)}扣底；扣牌提交前仍可补亮反。`;
  if (state.phase === 'playing') {
    if(!a.includes('play')) return `等待${name(state.next_seat)}出牌。你可以提前选牌。`;
    if (!state.current_lead) return '轮到你领出。可出单张、杠或同花色副牌甩牌；四张Q需选择真假杠。';
    if (state.current_lead.kind === 'true_gang') return `请跟${state.current_lead.cards.length}张：真杠先跟主牌，不足再补副牌；也可出更大的真杠。`;
    if (state.current_lead.kind === 'false_gang') return `请跟${state.current_lead.cards.length}张：假杠先跟副牌，不足再补主牌；也可出真杠。`;
    return `轮到你跟${state.current_lead.cards.length}张牌，有领出花色的牌须先跟足。`;
  }
  return '本局结束。点击“下一局”，由结算决定下局庄家和进贡，再重新准备。';
}
function renderSeats() {
  const played=new Set([...(state.trick_history||[]).flatMap(t=>t.plays.flatMap(p=>p.cards)),...state.current_plays.flatMap(p=>p.cards)]);
  for (const p of state.players) {
    const position=relativeSeat(p.seat);
    const seat = $(`seat-${position}`);
    const active = state.phase === 'playing' && state.next_seat === p.seat;
    const reveals=(state.reflections||[]).filter(r=>r.seat===p.seat);
    const key=JSON.stringify([p,active,state.phase,state.dealer,state.hand_counts[p.seat],state.ready_seats.includes(p.seat),state.confirmed_seats.includes(p.seat),reveals,[...played],state.bottom_cards]);
    if(seat.dataset.renderKey===key)continue;seat.dataset.renderKey=key;seat.replaceChildren();
    seat.className = `seat seat-${['bottom','right','top','left'][position]}${p.seat%2!==ourTeam()?' enemy':''}${p.seat===me()?' myself':''}${active?' active':''}`;
    seat.setAttribute('aria-label',`${p.seat+1}号座位，${p.name}${p.seat===me()?'，你':position===2?'，你的搭档':''}`);
    const info = element('div'); const title = element('div','seat-name',p.name);
    title.title=p.name;
    if (state.dealer === p.seat) title.append(element('span','dealer-tag','庄'));
    if (p.owner) title.append(element('span','dealer-tag','房主'));
    if(position===2)title.append(element('small','seat-team','你的搭档'));
    let status = state.phase === 'waiting' ? (state.ready_seats.includes(p.seat)?'已准备':'未准备') : `${state.hand_counts[p.seat]}张`;
    if (state.phase === 'declaring') status = state.confirmed_seats.includes(p.seat)?'已确认':'待确认';
    if (state.phase === 'settled') status = '本局结束';
    if(friends) status=p.occupied?`${status} · ${p.online?'在线':'离线，保留座位'}`:'等待朋友';
    const meta = element('div','seat-meta',status);
    if (active) {meta.prepend(element('span','turn-pip'));const timer=element('strong','turn-countdown');timer.dataset.turnSeat=p.seat;meta.append(timer);}
    if (p.seat !== me() && state.hand_counts[p.seat] > 0 && state.phase !== 'declaring') meta.append(element('span','cardback'));
    const interact=(!friends||p.occupied)&&p.seat!==me();
    const avatar=element(interact?'button':'div',`avatar${interact?' interaction-avatar':''}`,Array.from(p.name)[0]||'?');
    if(interact){avatar.type='button';avatar.title=`与${p.name}互动`;avatar.setAttribute('aria-label',`与${p.name}互动`);avatar.addEventListener('click',()=>openPlayer(p.seat));}
    info.append(title,meta); seat.append(avatar,info);
    if(reveals.length){
      const badges=element('div','seat-reflections');
      for(const r of reveals){
        const label=r.cards[0].split(':')[1]==='3'?'三反':'五反';
        const badge=element('div','reflection-badge'),faces=element('div','reflection-faces');
        badge.append(element('small','reflection-label',label));
        for(const id of r.cards){
          const face=cardNode(id,true),used=played.has(id),buried=state.bottom_cards.includes(id);
          face.classList.add('reflection-card');face.classList.toggle('reflection-used',used||buried);
          face.dataset.card=id;face.dataset.used=String(used||buried);
          face.title=cardLabel(id)+(used?' · 已出':buried?' · 已扣底':' · 未出');
          face.setAttribute('aria-label',face.title);faces.append(face);
        }
        badge.append(faces);
        badge.setAttribute('aria-label',`已亮${label}：${r.cards.map(cardLabel).join('、')}`);
        badges.append(badge);
      }
      seat.append(badges);
    }
  }
}
function renderCenter() {
  const center = $('table-center'); center.replaceChildren();
  center.classList.toggle('center-result',state.phase==='settled');
  let title='',subtitle='',kicker='';
  if (state.phase === 'waiting') { center.append(element('div','center-watermark','35')); title=friends?(state.players.filter(p=>p.occupied).length<4?'等朋友入座':'四人准备，开局'):'等你入座'; subtitle='对家携手，闲家争分'; kicker='四人 · 一副牌'; }
  else if(state.phase === 'playing') {
    if (state.current_plays.length) {
      center.append(element('div','trick-info',`第${state.trick_count+1}轮 · ${KINDS[state.current_lead.kind]}`));
      center.append(element('p','',`轮到${name(state.next_seat)}`)); return;
    } else if (state.last_trick) {
      center.append(element('div','trick-info',`上一轮 · ${name(state.last_trick.winner)}收${state.last_trick.points}分`));
      center.append(element('p','',`第${state.trick_count+1}轮由${name(state.next_seat)}领出`)); return;
    }
    title='开打';subtitle=`庄家${name(state.dealer)}先出`;kicker='第一轮';
  } else if(state.phase === 'settled') {
    center.classList.add('center-result'); const result=state.result.settlement;
    center.append(element('div','center-kicker',`第${state.deal_number}局 · ${state.result.surrender_by!==null?'投降结束':'已结束'}`),element('h2','',result.winning_team===ourTeam()?'我们赢了':'对方获胜'),element('div','result-score',`${state.team_points[ourTeam()]} : ${state.team_points[1-ourTeam()]}`),element('p','',`闲家${result.defender_points}分 · 下局${name(result.next_dealer)}坐庄`),element('p','',`下局${result.tribute_obligations.length?'需进贡'+result.tribute_obligations.length+'人':'无进贡义务'}`));
    const finish=element('button','secondary-button','整场结算');finish.disabled=busy||Boolean(state.proposal);finish.addEventListener('click',()=>startVoteDialog('match_end'));center.append(finish);return;
  } else if(state.phase === 'dealing') { title='发牌中';subtitle=`手牌已发${state.dealt_count} / 48张`;kicker=state.called_two?`${SUIT_NAMES[state.trump_suit]}为主`:'拿到2，可抢先亮出'; }
  else if(state.phase === 'declaring') {title='亮三五反';subtitle=`${state.confirmed_seats.length} / 4人已确认`;kicker='亮反后，再进贡';}
  else if(state.phase === 'tribute_give' || state.phase === 'tribute_return') {title=PHASES[state.phase];subtitle='选贡牌，完成双方交换';kicker='先贡后拿底';}
  else if(state.phase === 'discard' || state.phase === 'take_bottom') {title=PHASES[state.phase];subtitle=`庄家：${name(state.dealer)}`; const reasons={reflection:'亮反免贡',blackout:'断电免贡',no_eligible_giver:'无合适贡牌，全部免贡',no_eligible_return:'无合法还贡牌，全部免贡',no_obligation:'本局无进贡义务'};kicker=reasons[state.exemption] || '贡牌交换已完成';}
  else {title=PHASES[state.phase];subtitle='稍候，牌局继续';kicker='主花色待定';}
  center.classList.remove('center-result');center.append(element('div','center-kicker',kicker),element('h2','',title),element('p','',subtitle));
}
function renderPlays() {
  const current=state.current_plays.length>0;
  const plays=current?state.current_plays:(state.last_trick?.plays || []);
  const key=JSON.stringify([plays,current,state.last_trick?.winner]); if(key===playKey) return;playKey=key;
  for(let seat=0;seat<4;seat++) {
    const zone=$(`play-${relativeSeat(seat)}`);zone.replaceChildren();zone.classList.toggle('winning-card',!current && state.last_trick?.winner===seat);
    const play=plays.find(p=>p.seat===seat); if(!play) continue;
    for(const id of play.cards) zone.append(cardNode(id,true,publicReflection(id)));
    zone.append(element('span','play-caption',`第${state.trick_count+(current?1:0)}轮`));
  }
}
function renderHand() {
  const nextKey=JSON.stringify(state.hand_details);
  if(nextKey!==handKey) {
    const focused=document.activeElement?.dataset.card;handKey=nextKey;
    $('hand').replaceChildren(...state.hand_details.map((d,i)=>{const node=cardNode(d.id,false,d,true);node.style.zIndex=i;return node;}));
    if(focused) [...$('hand').children].find(n=>n.dataset.card===focused)?.focus({preventScroll:true});
  }
  $('hand-count').textContent=`${state.hand.length}张`;
  $('hand').dataset.empty=state.phase==='settled'?'手牌已打完，下一局再战。':state.phase==='dealing'?'正在发牌…':'准备好后，手牌会发到这里。';
  if(state.phase==='settled') $('hand').setAttribute('aria-label','本局手牌已打完');else $('hand').setAttribute('aria-label',`你的${state.hand.length}张手牌`);
  fitHand();
}
function validSelection(actionType) {
  const details=state.hand_details.filter(c=>selected.has(c.id));
  const ranks=details.map(c=>c.id.split(':')[1]);
  if(actionType==='call_trump') return details.length===1 && ranks[0]==='2';
  if(actionType==='reveal') return details.length===3 && ['3','5'].includes(ranks[0]) && ranks.every(r=>r===ranks[0]) && details.every(d=>!d.reflected&&!d.exchanged);
  if(actionType==='give_tribute') return details.length===1 && details[0].trump;
  if(actionType==='return_tribute') { const gift=state.exchanges.find(e=>e.receiver===me())?.gift;return details.length===1&&details[0].trump&&!details[0].points&&!details[0].reflected&&details[0].id!==gift; }
  if(actionType==='discard') return details.length===6 && details.every(d=>!d.points);
  if(actionType==='play') {
    if(state.current_lead) return details.length===state.current_lead.cards.length;
    if(details.length===4&&ranks.every(r=>r==='Q')) return Boolean($('kind').value);
    return details.length>0;
  }
  return true;
}
function renderSelection() {
  if(!state) return;
  for(const node of $('hand').children) {
    const isSelected=selected.has(node.dataset.card);node.classList.toggle('selected',isSelected);
    node.setAttribute('aria-pressed',String(isSelected));node.disabled=state.auto || Boolean(state.fault);
  }
  $('selection-count').textContent=selected.size?`已选 ${selected.size} 张`:'未选牌';
  $('clear').disabled=!selected.size||busy;
  $('hint').disabled=!state.hint||state.auto||busy||Boolean(state.fault);
  const showKind=state.phase==='playing'&&!state.current_lead&&selected.size===4;
  $('kind-wrap').hidden=!showKind;if(!showKind) $('kind').value='';
  for(const button of $('actions').children) button.disabled=busy||state.auto||Boolean(state.fault)||!validSelection(button.dataset.action);
  $('auto').disabled=busy||state.phase==='settled'||Boolean(state.fault);
  $('auto').textContent=state.auto?'接手本局':'托管本局';$('auto').classList.toggle('is-auto',state.auto);
  $('auto-menu').disabled=$('auto').disabled;$('auto-menu').textContent=$('auto').textContent;
  $('reset').disabled=busy || (friends&&!state.fault&&!['waiting','settled'].includes(state.phase));
  renderSpecialHints();
  if($('player-dialog').open)updatePlayerDialog();
  updateSessionButtons();
}
function renderActions() {
  let actions=[...state.available_actions];
  // Leave the contextual card action visible before a valid selection is available.
  if(state.phase==='dealing'&&!state.called_two&&!actions.includes('call_trump')) actions.push('call_trump');
  const key=actions.join('|');
  if(key!==actionKey) {
    actionKey=key;const focused=document.activeElement?.dataset.action;
    $('actions').replaceChildren(...actions.map((a,i)=>{
      const node=element('button',['reveal','change_seat','unready','revolution'].includes(a)?'secondary-button':'primary-button',LABELS[a]);node.type='button';node.dataset.action=a;node.addEventListener('click',()=>a==='revolution'?showDialog('revolution-dialog'):action(a));return node;
    }));
    if(focused) [...$('actions').children].find(n=>n.dataset.action===focused)?.focus({preventScroll:true});
  }
  $('instruction').textContent=instruction();
}
function render() {
  $('deal-label').textContent=state.phase==='waiting'?(state.deal_number?`第 ${String(state.deal_number+1).padStart(2,'0')} 局 · 待开局`:`准备开局 · ${friends?'好友房':'练习桌'}`):`第 ${String(state.deal_number).padStart(2,'0')} 局 · ${friends?'好友房':'练习桌'}`;
  $('trump-label').textContent=state.trump_suit?`${SUITS[state.trump_suit]} ${SUIT_NAMES[state.trump_suit]}为主`:'主花色待定';
  if(state.trump_suit&&state.caller!==null)$('trump-label').append(element('small','trump-caller',`${name(state.caller)}${state.called_two?'亮2':'抽底'}定主`));
  $('phase-label').textContent=state.phase==='playing'?`第${state.trick_count+1}轮${state.current_lead?' · '+KINDS[state.current_lead.kind]:''}`:PHASES[state.phase];
  $('our-score').textContent=state.team_points[ourTeam()];$('their-score').textContent=state.team_points[1-ourTeam()];
  $('score-detail-ours').textContent=state.team_points[ourTeam()];$('score-detail-theirs').textContent=state.team_points[1-ourTeam()];
  $('our-role').textContent=state.dealer!==null?(state.dealer%2===ourTeam()?'庄家方':'闲家方'):'';
  $('their-role').textContent=state.dealer!==null?(state.dealer%2!==ourTeam()?'庄家方':'闲家方'):'';
  $('our-names').textContent=`${name(me())} · ${name((me()+2)%4)}`;
  $('their-names').textContent=`${name((me()+1)%4)} · ${name((me()+3)%4)}`;
  $('score-fill').style.width=`${state.team_points[ourTeam()]}%`;
  $('score-note').textContent=state.phase==='settled'?`本局闲家${state.result.settlement.defender_points}分。下局${name(state.result.settlement.next_dealer)}坐庄。`:'闲家40分起换庄，60分起有进贡。';
  renderSeats();renderCenter();renderPlays();renderHand();renderActions();renderSelection();renderHistory();
  document.title=state.phase==='playing'&&state.next_seat===me()?'轮到你出牌 · 三五反':'三五反 · 好友与练习牌桌';
  const logKey=state.events.join('\n');
  if(eventsKey!==logKey) {eventsKey=logKey;$('events').replaceChildren(...state.events.map(e=>element('li','',e)));$('events').scrollTop=$('events').scrollHeight;}
  $('bottom-panel').hidden=!state.bottom_cards.length;
  $('bottom-cards').replaceChildren(...state.bottom_cards.map(id=>cardNode(id,true,publicReflection(id))));
  if(state.fault) feedback(state.fault);
}

$('clear').addEventListener('click',()=>{selected.clear();feedback('');renderSelection();});
$('hint').addEventListener('click',()=>{if(!state?.hint)return;selected=new Set(state.hint.cards);$('kind').value='';feedback('已选出一组符合当前规则的牌。你可以调整后再提交。',true);renderSelection();});
$('kind').addEventListener('change',renderSelection);
$('auto').addEventListener('click',()=>action('auto'));
$('reset').addEventListener('click',()=>{$('tools-dialog').close();action('reset');});
$('auto-menu').addEventListener('click',()=>{$('tools-dialog').close();action('auto');});
$('rules-open').addEventListener('click',()=>showDialog('rules-dialog'));
for(const id of ['rules-close','rules-done']) $(id).addEventListener('click',()=>$('rules-dialog').close());
$('hand').addEventListener('keydown',event=>{
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  const cards=[...$('hand').children],i=cards.indexOf(document.activeElement);if(i<0)return;
  event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?cards.length-1:Math.max(0,Math.min(cards.length-1,i+(event.key==='ArrowRight'?1:-1)));cards[next].focus();
});
document.addEventListener('keydown',event=>{
  if(!state)return;
  if(event.key!=='Enter'||event.repeat||document.querySelector('dialog[open]')||['BUTTON','SELECT','INPUT'].includes(document.activeElement.tagName))return;
  const primary=[...$('actions').children].find(n=>n.classList.contains('primary-button')&&!n.disabled);if(primary){event.preventDefault();primary.click();}
});
async function roomCommand(command, extra={}) {
  if(busy)return;busy=true;requestEpoch++;
  $('lobby-feedback').textContent='';
  $('create-room').disabled=$('join-room').disabled=$('recover-seat').disabled=$('leave-room').disabled=true;
  const payload={command,...extra};if(command==='recover')payload.recovery_code=$('recover-input').value.trim();else if(['create','join'].includes(command)){payload.name=$('nickname').value.trim();if(command==='join')payload.room_code=$('room-code').value.trim();}
  try {
    const response=await fetch(`${api}/action`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const result=await response.json();
    if(response.ok){accept(result);if(command!=='emote')feedback('');}
    else {if(result.state)accept(result.state);$('lobby-feedback').textContent=result.error?.message||'操作失败';if(state)feedback(result.error?.message||'操作失败');}
  } catch { $('lobby-feedback').textContent='连接中断，请恢复后查看当前房间。';connection(false); }
  finally {busy=false;$('create-room').disabled=$('join-room').disabled=$('recover-seat').disabled=false;if(state){$('leave-room').disabled=!['waiting','settled'].includes(state.phase);renderSelection();renderSession();}}
}
function renderChat(){
  if(!friends||!state)return;
  const messages=state.chat_messages||[];
  if(chatRoom!==state.room_code){chatRoom=state.room_code;chatCursor=state.chat_sequence||0;chatUnread=0;chatKey='';}
  const fresh=messages.filter(m=>m.id>chatCursor&&m.seat!==me());
  if(!$('chat-dialog').open)chatUnread+=fresh.length;else chatUnread=0;
  chatCursor=Math.max(chatCursor,state.chat_sequence||0);
  $('chat-unread').hidden=!chatUnread;$('chat-unread').textContent=chatUnread>99?'99+':String(chatUnread);
  $('chat-open').setAttribute('aria-label',`房间聊天${chatUnread?`，${chatUnread}条新消息`:''}`);
  const key=JSON.stringify(messages);if(chatKey===key)return;chatKey=key;
  const list=$('chat-messages'),atBottom=list.scrollHeight-list.scrollTop-list.clientHeight<40;
  list.replaceChildren(...messages.map(m=>{
    const row=element('li',m.seat===me()?'chat-own':''),heading=element('div','chat-heading');
    heading.append(element('strong','',m.name),element('time','',new Date(m.at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})));
    row.append(heading,element('p','chat-text',m.text));return row;
  }));
  if(!messages.length)list.append(element('li','chat-empty','同桌消息会显示在这里。'));
  if(atBottom)list.scrollTop=list.scrollHeight;
}
$('chat-open').addEventListener('click',()=>{showDialog('chat-dialog');chatUnread=0;renderChat();$('chat-messages').scrollTop=$('chat-messages').scrollHeight;$('chat-input').focus();});
$('chat-form').addEventListener('submit',async event=>{
  event.preventDefault();if(!state||chatSending)return;
  const text=$('chat-input').value.trim();if(!text)return;
  const room=state.room_code;chatSending=true;$('chat-send').disabled=true;$('chat-feedback').textContent='';
  try{
    const response=await fetch(`${api}/action`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:'chat',text,room_code:room})});
    const result=await response.json();
    if(response.ok){accept(result);if(state?.room_code===room&&$('chat-input').value.trim()===text)$('chat-input').value='';$('chat-messages').scrollTop=$('chat-messages').scrollHeight;}
    else $('chat-feedback').textContent=result.error?.message||'发送失败，请重试';
  }catch{$('chat-feedback').textContent='连接中断，消息未确认发送，请稍后重试。';}
  finally{chatSending=false;$('chat-send').disabled=false;}
});
$('confirm-revolution').addEventListener('click',()=>{$('revolution-dialog').close();action('revolution');});
$('create-room').addEventListener('click',()=>roomCommand('create'));
$('join-room').addEventListener('click',()=>roomCommand('join'));
$('leave-room').addEventListener('click',()=>roomCommand('leave'));
$('recover-seat').addEventListener('click',()=>roomCommand('recover'));
$('copy-recovery').addEventListener('click',async()=>{
  try {await navigator.clipboard.writeText($('recovery-code').value);feedback('个人恢复码已复制，请只给自己保存。',true);}
  catch {$('recovery-code').select();feedback('请复制选中的个人恢复码，只给自己保存。',true);}
});
$('copy-invite').addEventListener('click',async()=>{
  try {await navigator.clipboard.writeText($('invite-link').value);feedback('邀请链接已复制。朋友需能访问同一台服务。',true);}
  catch {$('invite-link').select();feedback('请复制上方选中的邀请链接。',true);}
});
if(friends){document.body.classList.add('lobby-mode');$('lobby').hidden=false;$('game-layout').hidden=true;$('reset').hidden=true;$('mode-link').href='/';$('mode-link').textContent='单人练习';const code=new URL(location.href).searchParams.get('room');if(/^\d{6}$/.test(code||''))$('room-code').value=code;}
function renderSpecialHints() {
  const hints=state.special_hints||{reflections:[],gangs:[],revealed:[]};
  const labels=[...hints.reflections.map(r=>`有${r.rank==='3'?'三反':'五反'}${r.needs_choice?'待选择':''}`),...new Set(hints.gangs.map(g=>`有${KINDS[g.kind]}`))];
  $('special-open').textContent=labels.length?labels.join(' · '):hints.revealed.length?'已亮反':'特殊牌';
  $('special-open').classList.toggle('has-special',Boolean(labels.length));
  $('special-open').disabled=busy||!labels.length&&!hints.revealed.length;
  $('special-open').title=labels.join(' · ');$('special-open').setAttribute('aria-label',`查看特殊牌${labels.length?'，'+labels.join('，'):''}`);
  const key=JSON.stringify([hints,state.phase,state.available_actions.includes('reveal'),busy]);
  if(key===specialKey)return;specialKey=key;
  const panel=$('hand-hints');panel.replaceChildren();
  for(const r of hints.revealed)panel.append(element('span','special-badge',`已亮${r.rank==='3'?'三反':'五反'}`));
  for(const r of hints.reflections){
    const title=r.rank==='3'?'三反':'五反',group=element('div','reflection-hint');
    group.append(element('span','special-badge',r.needs_choice?`有${title} · 四张${r.rank}由你选择`:`有${title} · ${state.phase==='dealing'?'发完后自动亮':'扣牌前可亮出'}`));
    if(!r.needs_choice&&state.phase!=='dealing'&&state.available_actions.includes('reveal')){
      const button=element('button','hint-button',`亮出${title}`);button.disabled=busy;
      button.addEventListener('click',()=>{$('special-dialog').close();selected=new Set(r.choices[0].cards);renderSelection();action('reveal');});group.append(button);
    }
    if(r.needs_choice&&state.phase!=='dealing'&&state.available_actions.includes('reveal')){
      const alternatives=element('details','reflection-alternatives');alternatives.append(element('summary','', '其他亮法'));
      for(const [i,choice] of r.choices.entries()){
        const keep=choice.keep.split(':'),button=element('button','hint-button',`${i===0?'推荐：':''}保留${SUITS[keep[0]]}${keep[1]}，亮其余三张`);
        button.type='button';button.disabled=busy;
        button.addEventListener('click',()=>{$('special-dialog').close();selected=new Set(choice.cards);renderSelection();action('reveal');});
        (i===0?group:alternatives).append(button);
      }
      group.append(alternatives);
      group.append(element('small','', '也可保留四张真杠；确认后仍可补亮，庄家扣牌提交后截止。'));
    }
    panel.append(group);
  }
  for(const g of hints.gangs){
    const button=element('button','hint-button',`有${KINDS[g.kind]} · ${g.rank}`);button.type='button';button.disabled=busy;
    button.setAttribute('aria-label',`${KINDS[g.kind]}，${g.cards.map(cardLabel).join('、')}，点击选牌`);
    button.addEventListener('click',()=>{$('special-dialog').close();selected=new Set(g.cards);$('kind').value=g.kind;renderSelection();feedback('已选好这组杠，请按当前跟牌规则决定是否出牌。',true);});
    panel.append(button);
  }
}
function openPlayer(seat){
  playerTarget=state.players.find(p=>p.seat===seat);
  if(!playerTarget||playerTarget.seat===me()||(friends&&!playerTarget.occupied))return;
  $('kick-confirm').hidden=true;updatePlayerDialog();showDialog('player-dialog');
}
function updatePlayerDialog(){
  const p=state?.players.find(p=>p.seat===playerTarget?.seat);
  if(!p||(friends&&(!p.occupied||p.occupant_id!==playerTarget.occupant_id))){if($('player-dialog').open)$('player-dialog').close();return;}
  $('player-title').textContent=p.name;$('player-management').hidden=!state.is_owner;
  $('kick-player').disabled=busy||!state.can_kick;$('confirm-kick').disabled=busy||!state.can_kick;
  $('kick-tip').textContent=state.can_kick?'请离后全桌重新准备，该玩家当前身份需等房主重新开桌才能再次加入。':'只允许在开局前或本局结算后请离。';
  for(const button of document.querySelectorAll('[data-emote]'))button.disabled=busy;
}
function animateEmote(e){
  const table=$('table'),rect=table.getBoundingClientRect();
  const avatar=seat=>$(`seat-${relativeSeat(seat)}`).querySelector('.avatar');
  const from=avatar(e.from),to=avatar(e.to);if(!from||!to)return;
  const point=node=>{const r=node.getBoundingClientRect();return {x:r.left+r.width/2-rect.left-table.clientLeft,y:r.top+r.height/2-rect.top-table.clientTop};};
  const start=point(from),end=point(to),reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  const place=(node,p)=>{node.style.left=`${p.x}px`;node.style.top=`${p.y}px`;node.setAttribute('aria-hidden','true');table.append(node);};
  function impact(){
    if($('mute-emotes').checked)return;
    const splash=element('span',`emote-splash emote-${e.item}${reduced?' emote-still':''}`);
    if(reduced||!['egg','tomato'].includes(e.item))splash.textContent=EMOTES[e.item];
    else{
      splash.append(element('i','splash-center'));
      for(let i=0;i<7;i++){
        const drop=element('i','splash-drop'),angle=i*Math.PI*2/7;
        drop.style.setProperty('--sx',`${Math.cos(angle)*(26+i%2*9)}px`);drop.style.setProperty('--sy',`${Math.sin(angle)*(26+i%2*9)}px`);splash.append(drop);
      }
      for(let i=0;i<3&&e.item==='egg';i++){const shell=element('i','egg-shell');shell.style.setProperty('--shell-angle',`${i*120}deg`);splash.append(shell);}
    }
    place(splash,end);setTimeout(()=>splash.remove(),1000);
    if(!reduced)to.animate([{transform:'rotate(0)'},{transform:'rotate(-8deg)'},{transform:'rotate(7deg)'},{transform:'rotate(0)'}],{duration:300});
  }
  if(reduced){impact();return;}
  const projectile=element('span',`emote-projectile emote-${e.item}`,['egg','tomato'].includes(e.item)?'':EMOTES[e.item]);place(projectile,start);
  const dx=end.x-start.x,dy=end.y-start.y,distance=Math.hypot(dx,dy),arc=Math.min(105,Math.max(35,distance*.24),Math.max(0,Math.min(start.y,end.y)-24));
  const frames=Array.from({length:17},(_,i)=>{const p=i/16;return {offset:p,transform:`translate(calc(-50% + ${dx*p}px),calc(-50% + ${dy*p-4*arc*p*(1-p)}px)) rotate(${p*540}deg) scale(${.8+p*.2})`,opacity:Math.min(1,p*8+.3)};});
  const flight=projectile.animate(frames,{duration:Math.min(1150,850+distance*.5),easing:'linear',fill:'forwards'});
  flight.finished.then(()=>{projectile.remove();impact();},()=>projectile.remove());
  setTimeout(()=>{flight.cancel();projectile.remove();},2000);
}
function renderSocial(){
  for(const e of state.social_events||[]){
    if(e.id<=socialCursor)continue;socialCursor=e.id;
    if(Date.now()/1000-e.at>8||!EMOTES[e.item])continue;
    $('social-status').textContent=`${name(e.from)}向${name(e.to)}${EMOTE_LABELS[e.item]}`;
    feedback(`${name(e.from)}向${name(e.to)}${EMOTE_LABELS[e.item]}`,true);
    if($('mute-emotes').checked)continue;
    animateEmote(e);
  }
}
$('player-close').addEventListener('click',()=>$('player-dialog').close());
$('mute-emotes').addEventListener('change',()=>{try{localStorage.setItem('sanwufan-mute-emotes',$('mute-emotes').checked?'1':'0');}catch{}if($('mute-emotes').checked)for(const node of document.querySelectorAll('.emote-projectile,.emote-splash')){for(const animation of node.getAnimations())animation.cancel();node.remove();}});
for(const button of document.querySelectorAll('[data-emote]'))button.addEventListener('click',()=>{
  if(!state||!playerTarget||busy)return;
  const target=playerTarget.seat,target_id=playerTarget.occupant_id,item=button.dataset.emote;$('player-dialog').close();
  roomCommand('emote',{target,...(friends?{target_id}:{}),item,table_id:state.table_id});
});
$('kick-player').addEventListener('click',()=>{if(!state?.can_kick)return;$('kick-confirm').hidden=false;$('kick-question').textContent=`确定请离${playerTarget.name}吗？全桌将重新等待准备。`;});
$('cancel-kick').addEventListener('click',()=>$('kick-confirm').hidden=true);
$('confirm-kick').addEventListener('click',()=>{
  if(!state?.can_kick||busy)return;const target=playerTarget.seat,target_id=playerTarget.occupant_id;$('player-dialog').close();
  roomCommand('kick',{target,target_id,table_id:state.table_id,version:state.version});
});
function renderHistory(){
  const records=state.trick_history||[],deal=`${state.table_id}:${state.deal_number}`;
  $('history-open').disabled=!records.length;
  const key=JSON.stringify([deal,records,state.players.map(p=>p.name)]);
  if(key===historyKey)return;historyKey=key;
  $('history-plays').replaceChildren();
  $('history-result').textContent=records.length?`已完成 ${records.length} 轮 · 向下滚动查看`:'这一局还没有结束的轮次。';
  for(const record of records){
    const round=element('article','history-round-row'),heading=element('div','history-round-heading'),plays=element('div','history-round-plays');
    heading.append(element('strong','',`第${record.round}轮`),element('span','',`${KINDS[record.kind]}${record.throw_failed?' · 甩牌失败':''}`),element('span','history-round-score',`${name(record.winner)} +${record.points}分`));
    for(const [i,p] of record.plays.entries()){
      const row=element('div',`history-play${p.seat===record.winner?' history-winner':''}`),cards=element('div','history-cards');
      row.append(element('strong','',`${i+1}. ${name(p.seat)}`),element('small','',`${i===0?'领出':''}${i===0&&p.seat===record.winner?' · ':''}${p.seat===record.winner?'收牌':''}`));
      cards.append(...p.cards.map(id=>cardNode(id,true,publicReflection(id))));row.append(cards);plays.append(row);
    }
    round.append(heading,plays);$('history-plays').append(round);
  }
}
$('history-open').addEventListener('click',()=>{if(!state?.trick_history?.length)return;renderHistory();showDialog('history-dialog');});
$('history-close').addEventListener('click',()=>$('history-dialog').close());
for(const [button,id] of [['tools-open','tools-dialog'],['room-open','room-dialog'],['score-open','score-dialog'],['events-open','events-dialog'],['special-open','special-dialog']])$(button).addEventListener('click',()=>showDialog(id));
for(const button of document.querySelectorAll('[data-close]'))button.addEventListener('click',()=>$(button.dataset.close).close());
$('fullscreen').addEventListener('click',async()=>{
  $('tools-dialog').close();
  try{if(document.fullscreenElement)await document.exitFullscreen();else if(document.documentElement.requestFullscreen)await document.documentElement.requestFullscreen();else feedback('这个浏览器暂不支持全屏，可以直接在当前页面打牌。',true);}catch{feedback('未进入全屏，可以继续在当前页面打牌。',true);}
});
function updateTurnClock(){
  const active=state?.phase==='playing'&&state.turn_deadline!==null;
  const own=active&&state.next_seat===me();$('turn-alert').hidden=!own;
  if(!active){document.body.classList.remove('own-turn');return;}
  const seconds=Math.max(0,Math.ceil(state.turn_deadline-state.server_time-(performance.now()-clockReceived)/1000));
  $('turn-seconds').textContent=seconds?`${seconds}s`:'正在自动出牌';
  $('turn-alert').classList.toggle('urgent',seconds<=5);document.body.classList.toggle('own-turn',own);
  for(const badge of document.querySelectorAll('[data-turn-seat]')){badge.textContent=`${seconds}s`;badge.classList.toggle('urgent',seconds<=5);}
}
function updateSessionButtons(){
  const full=state&&(!friends||state.players.every(p=>p.occupied));
  $('surrender-start').disabled=busy||!full||Boolean(state?.fault)||Boolean(state?.proposal)||state?.phase!=='playing';
  $('match-end-start').disabled=busy||!full||Boolean(state?.fault)||Boolean(state?.proposal)||state?.phase!=='settled';
  $('match-open').disabled=!state;
  for(const id of ['vote-agree','vote-reject','vote-cancel','vote-start-confirm'])$(id).disabled=busy;
}
function renderSession(){
  updateTurnClock();updateSessionButtons();if(!state)return;
  const p=state.proposal;$('vote-banner').hidden=!p;
  if(p){
    $('vote-title').textContent=`${name(p.initiator)}发起${p.kind==='surrender'?'投降':'整场结算'}`;
    $('vote-progress').textContent=`${p.approved.length}/4 已同意`;
    $('vote-detail').textContent=state.players.map(player=>`${player.name}${p.approved.includes(player.seat)?' ✓':' · 等待'}`).join('　');
    $('vote-agree').hidden=p.approved.includes(me());$('vote-reject').hidden=p.initiator===me();$('vote-cancel').hidden=p.initiator!==me();
  }
  if($('vote-start-dialog').open&&((voteStartKind==='surrender'&&state.phase!=='playing')||(voteStartKind==='match_end'&&state.phase!=='settled')||p))$('vote-start-dialog').close();
  const latest=state.match_reports?.at(-1)?.id||null;
  if(latest&&latest!==reportCursor){reportCursor=latest;if(state.phase==='waiting'){renderMatch(latest);showDialog('match-dialog');}}
  if($('match-dialog').open)renderMatch($('match-picker').value);
}
function startVoteDialog(kind){
  if(!state||state.proposal||busy)return;voteStartKind=kind;
  $('vote-start-title').textContent=kind==='surrender'?'发起投降':'整场结算';
  $('vote-start-detail').textContent=kind==='surrender'?'四家均同意后，你所在的队判负；以通过时闲家已收分数决定进贡，未完成的一轮不计分。投票期间继续出牌，30秒计时照常。':'四家均同意后，保存这场的输赢、坐庄与连庄战绩，然后清除进贡义务，回到首局重新准备。任何一家拒绝即可继续下一局。';
  $('vote-practice-tip').hidden=friends;showDialog('vote-start-dialog');
}
function castVote(choice,kind=state?.proposal?.kind,id=state?.proposal?.id){
  if(!state||busy)return;
  roomCommand('table_vote',{table_id:state.table_id,kind,choice,proposal_id:id??null});
}
function renderMatch(preferred='current'){
  if(!state)return;
  const options=[{id:'current',label:'当前场 · 进行中',report:state.match_stats},...(state.match_reports||[]).slice().reverse().map((r,i)=>({id:r.id,label:`已结算第${state.match_reports.length-i}场 · ${r.total_deals}局`,report:r}))];
  const chosen=options.find(o=>o.id===preferred)||options[0];
  const key=JSON.stringify([chosen,options.map(o=>[o.id,o.label])]);if(matchKey===key)return;matchKey=key;
  $('match-picker').replaceChildren(...options.map(o=>{const node=element('option','',o.label);node.value=o.id;return node;}));$('match-picker').value=chosen.id;
  const report=chosen.report,content=$('match-content');content.replaceChildren();
  content.append(element('p','match-total',`${report.total_deals} 局已结束 · ${report.surrender_deals} 局投降${report.ended?' · 已完成整场结算':''}`));
  const teams=element('div','match-teams');
  for(const team of report.teams){const box=element('section');box.append(element('small','',report.players.filter(p=>p.seat%2===team.team).map(p=>p.name).join(' / ')),element('strong','',`${team.wins}胜 ${team.losses}负`),element('span','',`累计收分 ${team.points}`));teams.append(box);}content.append(teams);
  const table=element('table','match-table'),heading=element('tr');for(const label of ['玩家','胜 / 负','坐庄','留庄','最长连庄'])heading.append(element('th','',label));const head=element('thead');head.append(heading);table.append(head);
  const body=element('tbody');for(const p of report.players){const row=element('tr');for(const value of [p.name,`${p.wins} / ${p.losses}`,p.dealer_deals,p.retained_deals,p.longest_streak])row.append(element('td','',value));body.append(row);}table.append(body);content.append(table);
  const details=element('details','match-deals');details.append(element('summary','',`逐局结果（${report.total_deals}局）`));const list=element('ol');
  for(const [i,r] of report.deals.entries()){const winners=report.players.filter(p=>p.seat%2===r.settlement.winning_team).map(p=>p.name).join(' / ');list.append(element('li','',`${i+1}. ${winners}胜 · 庄家${report.players[r.dealer].name} · 闲家${r.settlement.defender_points}分${r.surrender_by!==null?' · 投降':''}`));}details.append(list);content.append(details);
}
$('surrender-start').addEventListener('click',()=>startVoteDialog('surrender'));
$('match-end-start').addEventListener('click',()=>startVoteDialog('match_end'));
$('vote-start-confirm').addEventListener('click',()=>{$('vote-start-dialog').close();castVote('start',voteStartKind,null);});
for(const choice of ['agree','reject','cancel'])$(`vote-${choice}`).addEventListener('click',()=>castVote(choice));
$('match-open').addEventListener('click',()=>{renderMatch();showDialog('match-dialog');});
$('match-picker').addEventListener('change',()=>renderMatch($('match-picker').value));
setInterval(updateTurnClock,200);
document.addEventListener('fullscreenchange',()=>{$('fullscreen').textContent=document.fullscreenElement?'退出全屏':'进入全屏';fitHand();});
poll();
