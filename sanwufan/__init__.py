"""三五反规则引擎与完整牌局的公共接口。"""
from .models import Card, PlayKind, Reflection, RuleContext, RuleViolation, Suit, deck
from .rules import (
    Lead, declare_reflection, group, is_false_gang, is_true_gang, is_trump,
    prepare_lead, strength, trick_winner, validate_bottom, validate_follow,
)
from .settlement import Settlement, Tribute, settle
from .trick import Play, Trick, TrickResult
from .game import DealResult, Exchange, FirstNoTrump, Game, GameOptions, LateReflection, Phase

__all__ = [
    "Card", "PlayKind", "Reflection", "RuleContext", "RuleViolation", "Suit", "deck",
    "Lead", "declare_reflection", "group", "is_false_gang", "is_true_gang", "is_trump",
    "prepare_lead", "strength", "trick_winner", "validate_bottom", "validate_follow",
    "Settlement", "Tribute", "settle", "Play", "Trick", "TrickResult",
    "DealResult", "Exchange", "FirstNoTrump", "Game", "GameOptions", "LateReflection", "Phase",
]
