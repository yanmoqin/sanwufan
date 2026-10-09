"""Shared server-side turn clock, unanimous votes and match statistics."""
from itertools import combinations
import secrets
import time

from .game import Game, Phase
from .models import RuleViolation, validate_seat
from .rules import validate_follow

TURN_SECONDS = 30
SESSION_FIELDS = ('turn_key', 'turn_deadline', 'proposal', 'vote_sequence',
                  'match_started', 'match_ledger', 'match_reports')


def random_legal_play(game, seat, rng=None):
    """Random single when leading, random legal combination when following.

    Uses only the player's hand and public lead, with no opponent-hand strategy.
    """
    hand = game.hands[seat]
    if game.trick.lead is None:
        choices = [(c,) for c in hand]
    else:
        choices = []
        for selected in combinations(hand, len(game.trick.lead.cards)):
            try:
                choices.append(validate_follow(hand, selected, game.trick.lead, game.context))
            except RuleViolation:
                continue
    if not choices:
        raise RuntimeError('没有找到合法超时出牌')
    return (rng.choice if rng else secrets.choice)(choices)


def match_summary(ledger, names, started, ended=None):
    players = [dict(seat=s, name=n, wins=0, losses=0, dealer_deals=0,
                    retained_deals=0, longest_streak=0) for s, n in enumerate(names)]
    runs = [0] * 4
    last_dealer = None
    teams = [dict(team=t, wins=0, losses=0, points=0) for t in range(2)]
    for row in ledger:
        dealer, winner = row['dealer'], row['settlement']['winning_team']
        players[dealer]['dealer_deals'] += 1
        for s, player in enumerate(players):
            player['wins' if s % 2 == winner else 'losses'] += 1
        for t, team in enumerate(teams):
            team['wins' if t == winner else 'losses'] += 1
            team['points'] += row['team_points'][t]
        if last_dealer != dealer:
            runs = [0] * 4
        if winner == dealer % 2:
            runs[dealer] += 1
            players[dealer]['retained_deals'] += 1
            players[dealer]['longest_streak'] = max(players[dealer]['longest_streak'], runs[dealer])
        else:
            runs = [0] * 4
        last_dealer = dealer
    return dict(total_deals=len(ledger), surrender_deals=sum(r['surrender_by'] is not None for r in ledger),
                started=started, ended=ended, teams=teams, players=players,
                deals=[dict(r) for r in ledger])


class TableSession:
    def _init_session(self):
        self.turn_key = None
        self.turn_deadline = None
        self.proposal = None
        self.vote_sequence = 0
        self.match_started = time.time()
        self.match_ledger = []
        self.match_reports = []

    def _reset_session(self, keep_reports=True):
        self.turn_key = self.turn_deadline = self.proposal = None
        self.match_started = time.time()
        self.match_ledger = []
        if not keep_reports:
            self.match_reports = []

    def _sync_turn_clock(self):
        g = self.game
        key = (self.table_id, g.number, len(g.tricks), len(g.trick.plays), g.trick.next_seat) if g.phase == Phase.PLAYING else None
        if key != self.turn_key:
            self.turn_key = key
            self.turn_deadline = time.time() + TURN_SECONDS if key else None

    def _check_play_deadline(self):
        self._sync_turn_clock()
        if self.turn_deadline is not None and time.time() >= self.turn_deadline:
            raise RuleViolation('TURN_EXPIRED', '30秒已到，服务器正在随机出合法牌，请等待牌桌更新')

    def _timeout_turn(self):
        if self.fault:
            return False
        self._sync_turn_clock()
        if self.turn_deadline is None or time.time() < self.turn_deadline:
            return False
        seat = self.game.trick.next_seat
        cards = random_legal_play(self.game, seat, self.rng)
        self._perform(seat, 'play', cards)
        self._log(f'{self.names[seat]}30秒未出牌，已随机出一组合法牌。')
        return True

    def _session_committed(self, previous):
        self._sync_turn_clock()
        if self.game.phase == Phase.SETTLED and previous.phase != Phase.SETTLED:
            self.match_ledger = self.match_ledger + [self.game.result.to_dict()]
        if self.proposal:
            valid = (self.proposal['table_id'] == self.table_id and
                     self.proposal['deal_number'] == self.game.number and
                     self.game.phase == (Phase.PLAYING if self.proposal['kind'] == 'surrender' else Phase.SETTLED))
            if not valid:
                self.proposal = None

    def table_vote(self, seat, payload):
        """Seat comes from the authenticated session, never from the request."""
        with self.lock:
            validate_seat(seat)
            if set(payload) != {'command', 'table_id', 'choice', 'kind', 'proposal_id'}:
                raise RuleViolation('INVALID_REQUEST', '投票请求格式不正确')
            if payload['table_id'] != self.table_id:
                raise RuleViolation('STATE_CHANGED', '牌桌已变更，请按最新状态投票')
            if hasattr(self, 'members') and None in self.members:
                raise RuleViolation('NOT_ALL_SEATED', '需要四家均已入座才能投票')
            choice, kind = payload['choice'], payload['kind']
            if kind not in ('surrender', 'match_end') or choice not in ('start', 'agree', 'reject', 'cancel'):
                raise RuleViolation('INVALID_VOTE', '不支持这种投票')
            required = Phase.PLAYING if kind == 'surrender' else Phase.SETTLED
            if self.fault or self.game.phase != required:
                raise RuleViolation('WRONG_PHASE', '投降须在出牌时发起；整场结算须在一局结束后发起')
            if choice == 'start':
                if self.proposal or payload['proposal_id'] is not None:
                    raise RuleViolation('VOTE_IN_PROGRESS', '已有投票正在进行，请先处理')
                self.vote_sequence += 1
                self.proposal = dict(id=self.vote_sequence, kind=kind, table_id=self.table_id,
                                     deal_number=self.game.number, initiator=seat, approved=[seat])
                self._log(f"{self.names[seat]}发起{'投降' if kind == 'surrender' else '整场结算'}，等待四家同意。")
            else:
                p = self.proposal
                if not p or type(payload['proposal_id']) is not int or payload['proposal_id'] != p['id'] or kind != p['kind']:
                    raise RuleViolation('STALE_VOTE', '这项投票已结束或变更')
                if choice == 'cancel' and seat != p['initiator']:
                    raise RuleViolation('INITIATOR_ONLY', '只有发起者可以撤回投票')
                if choice in ('reject', 'cancel'):
                    self.proposal = None
                    self._log(f"{self.names[seat]}{'拒绝' if choice == 'reject' else '撤回'}了投票，继续牌局。")
                elif seat not in p['approved']:
                    self.proposal = {**p, 'approved': sorted(p['approved'] + [seat])}
            # Practice bots consent immediately; human rooms require four requests.
            if self.proposal and not hasattr(self, 'members'):
                self.proposal = {**self.proposal, 'approved': [0, 1, 2, 3]}
            self.version += 1
            if self.proposal and len(self.proposal['approved']) == 4:
                p = self.proposal
                self.proposal = None
                if kind == 'surrender':
                    self._commit(self.game.surrender(p['initiator']),
                                 f"四家同意投降，{self.names[p['initiator']]}所在队判负；按已收分数结算进贡。")
                else:
                    report = match_summary(self.match_ledger, self.names, self.match_started, time.time())
                    report['id'] = secrets.token_hex(12)
                    self.match_reports = (self.match_reports + [report])[-30:]
                    self.game = Game(options=self.game.options)
                    self.table_id = secrets.token_hex(12)
                    self.auto = False
                    self.two_seen = {}
                    self.claim_deadline = None
                    self._reset_session()
                    self._log(f"四家同意整场结算，共完成{report['total_deals']}局。战绩已保存，可以重新准备。")

    def _session_view(self):
        self._sync_turn_clock()
        return dict(turn_limit=TURN_SECONDS, turn_deadline=self.turn_deadline, server_time=time.time(),
                    proposal={**self.proposal, 'approved': list(self.proposal['approved'])} if self.proposal else None,
                    match_stats=match_summary(self.match_ledger, self.names, self.match_started),
                    match_reports=list(self.match_reports))
