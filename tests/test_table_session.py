from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sanwufan.friends import FriendRooms
from sanwufan.game import DealResult, Phase
from sanwufan.models import RuleViolation
from sanwufan.models import PlayKind
from sanwufan.practice import PracticeRoom, legal_play, suggested_cards
from sanwufan.settlement import settle_surrender
from sanwufan.storage import SQLiteStore, StorageError, decode, encode
from sanwufan.table_session import match_summary, random_legal_play
from tests.test_game import cards, declarations_done, fixed_order, prepared
from tests.test_revolution_chat import scoreless_game


def playing_game():
    g = declarations_done(prepared())
    g = g.take_bottom(g.dealer)
    return g.discard(g.dealer, suggested_cards(g, g.dealer, 'discard'))


class TableFixture:
    def setUp(self):
        self.registry = FriendRooms(seed=17)
        self.tokens = [self.registry.session(None)[0] for _ in range(5)]
        state = self.registry.request(self.tokens[0], {'command': 'create', 'name': '甲'})
        self.code = state['room_code']
        for i in range(1, 4):
            self.registry.request(self.tokens[i], {'command': 'join', 'name': f'朋友{i}', 'room_code': self.code})
        self.room = self.registry.rooms[self.code]
        self.room._commit(playing_game())

    def vote(self, seat, choice='start', kind='surrender', proposal_id=None):
        if proposal_id is None and choice != 'start' and self.room.proposal:
            proposal_id = self.room.proposal['id']
        return self.registry.request(self.tokens[seat], dict(command='table_vote', table_id=self.room.table_id,
                                    kind=kind, choice=choice, proposal_id=proposal_id))

    def finish_surrender(self, initiator=0):
        self.vote(initiator)
        for seat in range(4):
            if seat != initiator:
                result = self.vote(seat, 'agree')
        return result


class TableSessionTests(TableFixture, unittest.TestCase):
    def test_requires_four_different_authenticated_approvals(self):
        self.vote(0)
        self.vote(1, 'agree')
        self.vote(1, 'agree')
        self.vote(2, 'agree')
        self.assertEqual(self.room.proposal['approved'], [0, 1, 2])
        self.assertEqual(self.room.game.phase, Phase.PLAYING)
        self.vote(3, 'agree')
        self.assertEqual(self.room.game.phase, Phase.SETTLED)
        self.assertEqual(self.room.game.result.settlement.winning_team, 1)
        self.assertEqual(len(self.room.match_ledger), 1)

    def test_reject_cancel_and_stale_proposal_leave_cards_unchanged(self):
        original = self.room.game
        self.vote(1)
        proposal_id = self.room.proposal['id']
        with self.assertRaises(RuleViolation):
            self.vote(2, 'cancel')
        self.vote(3, 'reject')
        self.assertIs(self.room.game, original)
        self.vote(1)
        with self.assertRaises(RuleViolation):
            self.vote(2, 'agree', proposal_id=proposal_id)
        self.vote(1, 'cancel')
        self.assertIsNone(self.room.proposal)

    def test_non_member_and_spoofed_seat_rejected(self):
        with self.assertRaises(RuleViolation):
            self.vote(4)
        self.vote(0)
        payload = dict(command='table_vote', table_id=self.room.table_id, kind='surrender',
                       choice='agree', proposal_id=self.room.proposal['id'], seat=3)
        with self.assertRaises(RuleViolation):
            self.registry.request(self.tokens[1], payload)
        self.assertEqual(self.room.proposal['approved'], [0])

    def test_vote_does_not_pause_clock_and_can_span_game_versions(self):
        deadline = self.room.turn_deadline
        self.vote(1)
        self.assertEqual(self.room.turn_deadline, deadline)
        seat = self.room.game.trick.next_seat
        self.room._perform(seat, 'play', legal_play(self.room.game, seat))
        self.vote(2, 'agree')
        self.assertEqual(self.room.proposal['approved'], [1, 2])

    def test_surrender_preserves_54_cards_excludes_incomplete_trick_and_next_deal_tribute(self):
        g = self.room.game
        self.room._perform(g.trick.next_seat, 'play', legal_play(g, g.trick.next_seat))
        before = self.room.game
        state = self.finish_surrender(0)
        self.room.game.check_invariants()
        self.assertEqual(self.room.game.hands, before.hands)
        self.assertEqual(self.room.game.trick, before.trick)
        self.assertEqual(state['trick_history'], [])
        self.assertEqual(state['team_points'], [0, 0])
        self.assertIsNone(state['turn_deadline'])
        decision = self.room.game.result.settlement
        self.assertTrue(all(t.giver % 2 == 0 for t in decision.tribute_obligations))
        next_game = self.room.game.next_deal()
        self.assertEqual(next_game.obligations, decision.tribute_obligations)
        self.assertEqual(next_game.dealer, decision.next_dealer)

    def test_score_is_taken_when_last_player_agrees(self):
        self.vote(0)
        self.vote(1, 'agree')
        self.vote(2, 'agree')
        while sum(self.room.game.team_points) == 0:
            g = self.room.game
            self.room._perform(g.trick.next_seat, 'play', legal_play(g, g.trick.next_seat))
        points = self.room.game.team_points
        self.vote(3, 'agree')
        self.assertEqual(self.room.game.result.team_points, points)

    def test_normal_deal_completion_clears_pending_surrender(self):
        self.vote(0)
        while self.room.game.phase == Phase.PLAYING:
            g = self.room.game
            self.room._perform(g.trick.next_seat, 'play', legal_play(g, g.trick.next_seat))
        self.assertIsNone(self.room.proposal)
        self.assertEqual(len(self.room.match_ledger), 1)

    def test_match_end_only_between_deals_blocks_next_then_keeps_report(self):
        with self.assertRaises(RuleViolation):
            self.vote(0, kind='match_end')
        self.finish_surrender(1)
        self.vote(2, kind='match_end')
        self.assertNotIn('next_deal', self.room.available(0))
        for seat in [0, 1]:
            self.vote(seat, 'agree', 'match_end')
        old_table = self.room.table_id
        view = self.vote(3, 'agree', 'match_end')
        self.assertNotEqual(old_table, view['table_id'])
        self.assertEqual(view['phase'], 'waiting')
        self.assertEqual(view['match_stats']['total_deals'], 0)
        report = view['match_reports'][-1]
        self.assertEqual(report['total_deals'], 1)
        self.assertEqual(report['players'][0]['wins'], 1)
        self.assertEqual(report['players'][1]['losses'], 1)
        self.assertEqual(view['room_code'], self.code)
        self.assertEqual(self.room.members, self.tokens[:4])

    def test_match_vote_rejected_restores_next_deal(self):
        self.finish_surrender()
        self.vote(1, kind='match_end')
        self.vote(2, 'reject', 'match_end')
        self.assertIn('next_deal', self.room.available(0))
        self.room._perform(0, 'next_deal')
        self.assertEqual(len(self.room.match_ledger), 1)

    def test_clock_is_stable_across_polls_chat_and_invalid_move(self):
        deadline = self.room.turn_deadline
        for _ in range(4):
            self.registry.state(self.tokens[0])
        self.room.chat(self.tokens[0], '测试计时')
        with self.assertRaises(RuleViolation):
            self.room._perform(self.room.game.trick.next_seat, 'play', ())
        self.assertEqual(self.room.turn_deadline, deadline)

    def test_timeout_plays_exactly_once_and_starts_next_clock(self):
        g = self.room.game
        old_deadline = self.room.turn_deadline
        self.room.next_tick = 10**20  # Turn expiry must ignore the dealing cadence.
        with patch('sanwufan.table_session.time.time', return_value=old_deadline):
            self.assertTrue(self.room.tick())
            self.assertFalse(self.room.tick())
        self.assertEqual(len(self.room.game.trick.plays), 1)
        played = self.room.game.trick.plays[0]
        self.assertIn(played.cards[0], g.hands[g.trick.next_seat])
        self.assertEqual(self.room.turn_deadline, old_deadline + 30)

    def test_manual_play_before_deadline_accepted_late_play_rejected(self):
        seat = self.room.game.trick.next_seat
        payload = dict(action='play', table_id=self.room.table_id, version=self.room.version,
                       cards=[c.id for c in legal_play(self.room.game, seat)])
        with patch('sanwufan.table_session.time.time', return_value=self.room.turn_deadline):
            with self.assertRaises(RuleViolation) as caught:
                self.registry.request(self.tokens[seat], payload)
        self.assertEqual(caught.exception.code, 'TURN_EXPIRED')
        with patch('sanwufan.table_session.time.time', return_value=self.room.turn_deadline - .01):
            self.registry.request(self.tokens[seat], payload)
        self.assertEqual(len(self.room.game.trick.plays), 1)

    def test_random_follow_is_legal_and_can_choose_different_cards(self):
        g = self.room.game
        seen = set()
        for _ in range(100):
            move = random_legal_play(g, g.trick.next_seat, self.room.rng)
            seen.add(move)
            g.play(g.trick.next_seat, move).check_invariants()
        self.assertGreater(len(seen), 1)
        g = g.play(g.trick.next_seat, legal_play(g, g.trick.next_seat))
        for _ in range(50):
            g.play(g.trick.next_seat, random_legal_play(g, g.trick.next_seat, self.room.rng)).check_invariants()

    def test_practice_supports_both_votes_with_automatic_bot_agreement(self):
        room = PracticeRoom(seed=4)
        room._commit(playing_game())
        def vote(kind):
            return room.action(dict(command='table_vote', table_id=room.table_id, kind=kind,
                                    choice='start', proposal_id=None))
        self.assertEqual(vote('surrender')['phase'], 'settled')
        self.assertEqual(vote('match_end')['match_reports'][0]['total_deals'], 1)

    def test_random_follow_handles_true_and_false_gang(self):
        wanted = cards('H:2', 'S:A', 'H:A', 'C:A', 'D:A', 'S:Q', 'H:7', 'C:7', 'D:7')
        g = declarations_done(prepared(order=fixed_order((wanted, (), (), ()))))
        g = g.take_bottom(0)
        fillers = tuple(c for c in g.hands[0] if not c.points and c not in wanted)[:6]
        g = g.discard(0, fillers)
        for chosen, kind in [(cards('S:A','H:A','C:A','D:A'), PlayKind.TRUE_GANG),
                             (cards('S:Q','H:7','C:7','D:7'), PlayKind.FALSE_GANG)]:
            with self.subTest(kind=kind):
                opened = g.play(0, chosen, kind)
                for _ in range(20):
                    opened.play(1, random_legal_play(opened, 1, self.room.rng)).check_invariants()

    def test_revolution_keeps_completed_statistics_but_reset_clears_current_series(self):
        self.finish_surrender()
        self.room._commit(scoreless_game())
        self.room._perform(0, 'revolution')
        self.assertEqual(len(self.room.match_ledger), 1)
        self.assertEqual(self.room.game.number, 1)
        # Explicit new-table reset (waiting/settled) begins a new series.
        self.room.game = playing_game().surrender(0)
        view = self.registry.state(self.tokens[0])
        self.registry.request(self.tokens[0], dict(action='reset', table_id=view['table_id'], version=view['version']))
        self.assertEqual(self.room.match_ledger, [])


class ScoreAndStatsTests(unittest.TestCase):
    def test_surrender_tiers_and_forced_loser_override_score_winner(self):
        for score, count in [(0, 2), (5, 0), (35, 0), (40, 0), (55, 0), (60, 1), (75, 1), (80, 2), (100, 2)]:
            for initiator in range(4):
                with self.subTest(score=score, initiator=initiator):
                    result = settle_surrender(0, score, initiator)
                    self.assertEqual(result.winning_team, 1 - initiator % 2)
                    self.assertEqual(len(result.tribute_obligations), count)
                    self.assertEqual(result.next_dealer, 0 if initiator % 2 else 1)
                    self.assertTrue(all(t.giver % 2 == initiator % 2 and t.receiver % 2 != initiator % 2
                                        for t in result.tribute_obligations))

    def test_long_match_counts_every_deal_and_streak_breaks_on_loss_or_dealer_change(self):
        ledger = []
        for i, (dealer, losing) in enumerate([(0, 1)] * 6 + [(0, 0), (1, 0), (1, 0), (1, 1)]):
            ledger.append(DealResult(i + 1, dealer, (10, 20), settle_surrender(dealer, 20, losing), 2, losing).to_dict())
        report = match_summary(ledger, ['甲', '乙', '丙', '丁'], 100, 200)
        self.assertEqual(report['total_deals'], 10)
        self.assertEqual(report['players'][0]['longest_streak'], 6)
        self.assertEqual(report['players'][1]['longest_streak'], 2)
        self.assertEqual(report['players'][0]['dealer_deals'], 7)
        self.assertEqual(report['teams'][0]['wins'], 7)
        self.assertEqual(report['teams'][0]['points'], 100)

    def test_previous_release_result_can_be_decoded(self):
        g = playing_game().surrender(0)
        old = encode(replace(g.result, surrender_by=None))
        del old['fields']['surrender_by']
        self.assertIsNone(decode(old).surrender_by)
        self.assertEqual(decode(encode(g)), g)


class SessionPersistenceTests(TableFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'session.sqlite3'
        self.registry.store = SQLiteStore(self.path)
        self.registry._save()

    def tearDown(self):
        self.registry.close()
        self.folder.cleanup()

    def restart(self):
        self.registry.store.close()
        self.registry = FriendRooms(store=SQLiteStore(self.path))
        self.room = self.registry.rooms[self.code]

    def test_pending_vote_deadline_survive_restart_and_expired_turn_plays(self):
        self.vote(0)
        deadline = self.room.turn_deadline
        self.restart()
        self.assertEqual(self.room.proposal['approved'], [0])
        self.assertEqual(self.room.turn_deadline, deadline)
        with patch('sanwufan.table_session.time.time', return_value=deadline + 1):
            self.registry.tick()
        self.restart()
        self.assertEqual(len(self.room.game.trick.plays), 1)
        self.assertEqual(self.room.proposal['approved'], [0])

    def test_final_vote_disk_failure_rolls_back_every_result_and_vote(self):
        self.vote(0)
        self.vote(1, 'agree')
        self.vote(2, 'agree')
        before = self.room.capture()
        with patch.object(self.registry.store, 'write', side_effect=StorageError('disk full')):
            with self.assertRaises(StorageError):
                self.vote(3, 'agree')
        self.assertEqual(self.room.capture(), before)
        self.restart()
        self.assertEqual(self.room.game.phase, Phase.PLAYING)
        self.vote(3, 'agree')
        self.restart()
        self.assertEqual(len(self.room.match_ledger), 1)
        self.assertEqual(self.room.game.phase, Phase.SETTLED)

    def test_report_survives_restart_and_start_new_series(self):
        self.finish_surrender()
        self.vote(0, kind='match_end')
        for seat in [1, 2, 3]:
            self.vote(seat, 'agree', 'match_end')
        report = self.room.match_reports[-1]
        self.restart()
        self.assertEqual(self.room.match_reports[-1], report)
        self.assertEqual(self.room.match_ledger, [])

    def test_final_match_vote_save_failure_preserves_previous_series(self):
        self.finish_surrender()
        self.vote(0, kind='match_end')
        self.vote(1, 'agree', 'match_end')
        self.vote(2, 'agree', 'match_end')
        before = self.room.capture()
        with patch.object(self.registry.store, 'write', side_effect=StorageError('disk full')):
            with self.assertRaises(StorageError):
                self.vote(3, 'agree', 'match_end')
        self.assertEqual(self.room.capture(), before)
        self.restart()
        self.assertEqual(self.room.game.phase, Phase.SETTLED)
        self.assertEqual(self.room.proposal['approved'], [0, 1, 2])

    def test_timeout_storage_failure_restores_rng_hand_and_deadline(self):
        before = self.room.capture()
        with patch('sanwufan.table_session.time.time', return_value=self.room.turn_deadline + 1):
            with patch.object(self.registry.store, 'write', side_effect=StorageError('disk full')):
                with self.assertRaises(StorageError):
                    self.registry.tick()
        self.assertEqual(self.room.capture(), before)

    def test_old_room_migrates_full_finished_history_into_match_ledger(self):
        self.finish_surrender()
        data = self.room.capture()
        for key in ['match_ledger', 'match_reports', 'turn_key', 'turn_deadline']:
            del data[key]
        self.room.restore(data)
        self.assertEqual(len(self.room.match_ledger), 1)
